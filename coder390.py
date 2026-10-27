#!/usr/bin/env python3
"""Build the pinned optimized engine and serve a user-owned Coder390 GGUF."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

from lib.gguf_header import read_metadata
from lib.util import digest, fingerprint, write

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.coder390'
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROFILES = {
    'compact32': {'context': 8192, 'batch': 512, 'ubatch': 128, 'output': 2048},
    'balanced': {'context': 32768, 'batch': 2048, 'ubatch': 512, 'output': 4096},
}


def read(path):
    return json.loads(path.read_text())


def clean_environment():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('LLAMA_', 'GGML_', 'MTL_', 'DYLD_', 'CMAKE_', 'MLX_'))
           and k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
           and k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CPATH',
                         'LIBRARY_PATH', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                         'HF_UI_VERSION', 'HF_WEBUI_VERSION')}
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               LLAMA_DRAFT_COMPACT_TOPK='1', TOKENIZERS_PARALLELISM='false')
    return env


def hardware():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('需要原生 Apple Silicon macOS；不支持 Intel、Windows 或 Rosetta Python')
    ram = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.memsize'], text=True))
    chip = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip()
    return {'chip': chip, 'memory_bytes': ram, 'macos': platform.mac_ver()[0]}


def model_check(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.gguf':
        raise ValueError('--model 需要指向已有的单文件 .gguf 主模型')
    metadata, tensors, _ = read_metadata(path)
    if metadata.get('general.architecture') != 'qwen35' or metadata.get('qwen35.nextn_predict_layers') != 1:
        raise ValueError('需要 Coder390 qwen35 架构且内置一个 MTP 层的 GGUF')
    contract = read(ROOT / 'model-contract.json')
    checks = {
        'tokenizer_sha256': fingerprint({k: v for k, v in metadata.items() if k.startswith('tokenizer.')}),
        'architecture_sha256': fingerprint({k: v for k, v in metadata.items() if k.startswith('qwen35.')}),
        'tensor_shapes_sha256': fingerprint({k: v['shape'] for k, v in tensors.items()}),
    }
    for key, value in checks.items():
        if value != contract[key]:
            raise ValueError('模型与已验证的 Coder390 契约不同：' + key + '；拒绝套用固定草稿词表')
    return {'path': str(path), 'size': path.stat().st_size,
            'mtime_ns': path.stat().st_mtime_ns, **checks,
            'mtp_ffn_types': {k: tensors[f'blk.64.ffn_{k}.weight']['ggml_type'] for k in ('gate', 'up', 'down')}}


def choose_profile(name, ram, model_size, context=None):
    name = ('compact32' if ram <= 36 * 2**30 else 'balanced') if name == 'auto' else name
    result = {'name': name, **PROFILES[name]}
    if ram < 32 * 2**30:
        raise ValueError('本发布包面向至少 32GB 统一内存的机器')
    if ram <= 36 * 2**30 and model_size > 20 * 2**30:
        raise ValueError('32GB 配置请使用约16.1GiB的 Q3 LynnStyle；该模型文件过大')
    if ram <= 36 * 2**30 and name != 'compact32':
        raise ValueError('32GB 机器请使用 compact32 配置')
    if context is not None:
        if context < 4096 or context > 262144:
            raise ValueError('--context 必须在4096～262144之间')
        if name == 'compact32' and context > 16384:
            raise ValueError('compact32 只允许4K～16K上下文；更长窗口未做32GB验收')
        result['context'] = context
    return result


def safe_relative(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Unsafe archive member: ' + name)
    return p


def unpack_tar(archive, destination, strip=0):
    """Extract bounded archives, rejecting traversal and external symlinks."""
    destination.mkdir(parents=True, exist_ok=True)
    links, seen, expanded = [], set(), 0
    with tarfile.open(archive, 'r:gz') as source:
        for item in source:
            relative = safe_relative(item.name)
            if len(relative.parts) <= strip:
                if item.isdir():
                    continue
                raise ValueError('Invalid archive root')
            relative = PurePosixPath(*relative.parts[strip:])
            if str(relative) in seen:
                raise ValueError('Duplicate archive member')
            seen.add(str(relative))
            target = destination / relative
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                expanded += item.size
                if expanded > 2 * 2**30:
                    raise ValueError('Archive exceeds expanded size limit')
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if item.mode & 0o111 else 0o644)
            elif item.issym():
                if PurePosixPath(item.linkname).is_absolute():
                    raise ValueError('Absolute archive symlink')
                resolved = (target.parent / item.linkname).resolve()
                if destination.resolve() not in resolved.parents:
                    raise ValueError('Escaping archive symlink')
                links.append((target, item.linkname))
            else:
                raise ValueError('Unsupported archive member')
    for target, link in links:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)


def unpack_cmake(archive, destination):
    with zipfile.ZipFile(archive) as source:
        if sum(x.file_size for x in source.infolist()) > 512 * 2**20:
            raise ValueError('CMake archive too large')
        for item in source.infolist():
            relative = safe_relative(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Unexpected wheel symlink')
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
    binary = destination / 'cmake/data/bin/cmake'
    binary.chmod(0o755)
    return binary


def package_fingerprint():
    lock = read(ROOT / 'engine.lock.json')
    for entry in [*lock['patches'], lock['vocab']]:
        if digest(ROOT / entry['path']) != entry['sha256']:
            raise RuntimeError('发布文件校验失败：' + entry['path'])
    return fingerprint(lock)


def engine_ready():
    receipt_path = DATA / 'engine.json'
    if not receipt_path.exists():
        return None
    receipt = read(receipt_path)
    if receipt.get('machine', {}).get('chip') != hardware()['chip']:
        return None
    if receipt['inputs'] != package_fingerprint():
        return None
    binary = DATA / 'build/bin/llama-server'
    ui = DATA / 'ui' / receipt['ui_relative']
    if not binary.is_file() or digest(binary) != receipt['binary_sha256']:
        return None
    if not ui.joinpath('index.html').is_file():
        return None
    if any(not (ui / name).is_file() or digest(ui / name) != sha for name, sha in receipt['ui_files'].items()):
        return None
    return binary, ui


def setup(args):
    machine = hardware()
    DATA.mkdir(exist_ok=True)
    with (DATA / 'setup.lock').open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ready = engine_ready()
        if ready:
            print('优化引擎和聊天界面已就绪。', flush=True)
            return ready
        if (DATA / 'engine.json').exists() and read(DATA / 'engine.json').get('machine', {}).get('chip') != machine['chip']:
            raise RuntimeError('已有原生构建属于另一种芯片；请在新克隆目录编译，不要复制别人的 .coder390 缓存')
        for tool in ('clang', 'make', 'git'):
            r = subprocess.run(['/usr/bin/xcrun', '--find', tool], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError('请先运行 xcode-select --install 并完成 Apple Command Line Tools 安装')
        if shutil.disk_usage(ROOT).free < 4 * 2**30:
            raise RuntimeError('至少需要4GiB空闲磁盘用于源码和编译缓存（不含模型）')
        lock = read(ROOT / 'engine.lock.json')
        inputs = package_fingerprint()
        cache = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else DATA / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        from lib import network, direct_transport
        archives = {}
        for key in ('source', 'cmake', 'ui'):
            item = lock[key]
            target = cache / item['filename']
            if target.is_file() and target.stat().st_size == item['size'] and digest(target) == item['sha256']:
                print('复用已校验文件：' + item['filename'], flush=True)
            else:
                if args.offline:
                    raise RuntimeError('离线缓存缺少或校验失败：' + str(target))
                direct_transport.configure(args.interface)
                print('直连下载构建组件：' + item['filename'], flush=True)
                network.fetch(item['url'], target, item['sha256'], item['size'])
            archives[key] = target
        source = DATA / 'source'
        source_receipt = DATA / 'source.json'
        source_inputs = fingerprint({key: lock[key] for key in ('source', 'patches', 'patched_files')})
        if not source.exists():
            with tempfile.TemporaryDirectory(prefix='source-', dir=DATA) as tmp:
                staging = Path(tmp) / 'tree'
                unpack_tar(archives['source'], staging, strip=1)
                patch_env = {**clean_environment(), 'GIT_CEILING_DIRECTORIES': str(DATA)}
                for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
                    patch_env.pop(key, None)
                for patch in lock['patches']:
                    subprocess.run(['/usr/bin/git', 'apply', '--check', str(ROOT / patch['path'])], cwd=staging, env=patch_env, check=True)
                    subprocess.run(['/usr/bin/git', 'apply', str(ROOT / patch['path'])], cwd=staging, env=patch_env, check=True)
                for name, sha in lock.get('patched_files', {}).items():
                    if digest(staging / name) != sha:
                        raise RuntimeError('补丁后的源码与已采用版本不符：' + name)
                staging.rename(source)
            write(source_receipt, {'inputs': source_inputs})
        elif not source_receipt.exists() or read(source_receipt)['inputs'] != source_inputs:
            raise RuntimeError('已有源码缓存与发布版本不同；请在新克隆目录准备，旧文件保留')
        for name, sha in lock.get('patched_files', {}).items():
            if digest(source / name) != sha:
                raise RuntimeError('源码缓存被修改：' + name)
        tools = DATA / 'cmake'
        if not tools.exists():
            with tempfile.TemporaryDirectory(prefix='tools-', dir=DATA) as tmp:
                staging = Path(tmp) / 'wheel'
                unpack_cmake(archives['cmake'], staging)
                staging.rename(tools)
        cmake = tools / 'cmake/data/bin/cmake'
        ui_root = DATA / 'ui'
        if not ui_root.exists():
            with tempfile.TemporaryDirectory(prefix='ui-', dir=DATA) as tmp:
                staging = Path(tmp) / 'tree'
                unpack_tar(archives['ui'], staging)
                staging.rename(ui_root)
        indexes = list(ui_root.rglob('index.html'))
        if len(indexes) != 1:
            raise RuntimeError('聊天界面包必须包含一个 index.html')
        ui = indexes[0].parent
        configure = [str(cmake), '-S', str(source), '-B', str(DATA / 'build'), '-G', 'Unix Makefiles',
                     *lock['cmake_options']]
        build = [str(cmake), '--build', str(DATA / 'build'), '--target', 'llama-server', '-j', str(args.jobs)]
        print('在本机编译优化引擎，日志：' + str(DATA / 'build.log'), flush=True)
        with (DATA / 'build.log').open('w') as log:
            for command in (configure, build):
                r = subprocess.run(command, env=clean_environment(), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
                if r.returncode:
                    raise RuntimeError('编译失败；查看 .coder390/build.log，已有文件保留用于诊断')
        binary = DATA / 'build/bin/llama-server'
        linked = subprocess.check_output(['/usr/bin/otool', '-L', str(binary)], text=True).splitlines()[1:]
        non_system = [line.strip().split(' ', 1)[0] for line in linked
                      if not line.strip().startswith(('/System/Library/', '/usr/lib/'))]
        if non_system:
            raise RuntimeError('构建意外依赖非系统动态库：' + ', '.join(non_system))
        help_text = subprocess.check_output([str(binary), '--help'], env=clean_environment(), text=True, stderr=subprocess.STDOUT)
        if '--spec-draft-vocab' not in help_text:
            raise RuntimeError('编译结果缺少草稿词表优化')
        receipt = {'inputs': inputs, 'machine': machine, 'binary_sha256': digest(binary),
                   'ui_relative': str(ui.relative_to(ui_root)), 'ui_files': {
                       str(f.relative_to(ui)): digest(f) for f in sorted(ui.rglob('*')) if f.is_file()},
                   'configure': configure, 'build': build, 'created_at': time.time()}
        write(DATA / 'engine.json', receipt)
        print('准备完成。模型权重没有下载或修改。', flush=True)
        return binary, ui


def command(binary, ui, model, profile, args):
    cmd = [str(binary), '--model', model['path'], '--host', '127.0.0.1', '--port', str(args.port),
           '--alias', 'coder390', '--ctx-size', str(profile['context']), '--parallel', '1',
           '--gpu-layers', 'all', '--flash-attn', 'on', '--cache-type-k', 'f16', '--cache-type-v', 'f16',
           '--spec-type', 'draft-mtp', '--spec-draft-n-max', '4', '--spec-draft-sampling', 'probabilistic',
           '--spec-draft-p-min', '0.2', '--spec-draft-vocab', str(ROOT / 'vocab/zh-code-131072.json'),
           '--threads', '8', '--threads-batch', '8', '--batch-size', str(profile['batch']),
           '--ubatch-size', str(profile['ubatch']), '--path', str(ui), '--metrics',
           '--ui-config', '{"showMessageStats":true,"keepStatsVisible":true}',
           '--jinja', '--reasoning-format', 'deepseek', '--offline', '--temp', '1.0',
           '--top-p', '0.95', '--top-k', '20', '--min-p', '0.0', '--repeat-penalty', '1.0',
           '--n-predict', str(profile['output'])]
    if args.mmproj:
        cmd.extend(['--mmproj', str(Path(args.mmproj).expanduser().resolve(strict=True))])
    return cmd


def run(args):
    machine = hardware()
    model = model_check(args.model)
    profile = choose_profile(args.profile, machine['memory_bytes'], model['size'], args.context)
    if args.mmproj and profile['name'] == 'compact32':
        raise ValueError('compact32 版本只验收文本模式；图像配置请在更大内存机器使用 balanced')
    print(json.dumps({'hardware': machine, 'profile': profile, 'model': model['path']}, ensure_ascii=False, indent=2), flush=True)
    DATA.mkdir(exist_ok=True)
    with (DATA / 'run.lock').open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', args.port))
        binary, ui = setup(args)
        cmd = command(binary, ui, model, profile, args)
        write(DATA / 'last-run.json', {'command': cmd, 'profile': profile, 'model': model, 'hardware': machine})
        proc = None
        previous = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        try:
            with (DATA / 'server.log').open('w', buffering=1) as log:
                proc = subprocess.Popen(cmd, env=clean_environment(), stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                write(DATA / 'active.json', {'pid': proc.pid, 'port': args.port, 'binary': str(binary)})
                for _ in range(180):
                    if proc.poll() is not None:
                        raise RuntimeError('模型加载失败，查看 .coder390/server.log')
                    try:
                        with CLIENT.open(f'http://127.0.0.1:{args.port}/health', timeout=1) as response:
                            if json.load(response).get('status') == 'ok':
                                owner = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(proc.pid),
                                                        f'-iTCP:{args.port}', '-sTCP:LISTEN'],
                                                       capture_output=True, text=True)
                                if owner.returncode == 0:
                                    break
                    except (OSError, ValueError):
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError('模型加载超时，查看 .coder390/server.log')
                print(f'已启动：http://127.0.0.1:{args.port}/  （保留此终端，Ctrl+C关闭）', flush=True)
                if not args.no_open:
                    subprocess.run(['/usr/bin/open', f'http://127.0.0.1:{args.port}/'], check=False)
                if proc.wait() != 0:
                    raise RuntimeError('模型进程异常退出，查看 .coder390/server.log')
        finally:
            signal.signal(signal.SIGTERM, previous)
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            (DATA / 'active.json').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('setup', 'run'):
        p = commands.add_parser(action)
        p.add_argument('--interface', help='构建组件下载网卡，如en0；默认检查系统物理网卡')
        p.add_argument('--cache-dir', help='可选已校验构建组件缓存，不是模型目录')
        p.add_argument('--offline', action='store_true', help='构建组件必须已在缓存中')
        p.add_argument('--jobs', type=int, choices=range(1, 17), default=8)
        if action == 'run':
            p.add_argument('--model', required=True)
            p.add_argument('--profile', choices=['auto', *PROFILES], default='auto')
            p.add_argument('--context', type=int)
            p.add_argument('--port', type=int, default=8793)
            p.add_argument('--mmproj')
            p.add_argument('--no-open', action='store_true')
    doctor = commands.add_parser('doctor')
    doctor.add_argument('--model')
    args = parser.parse_args()
    if args.action == 'setup':
        setup(args)
    elif args.action == 'run':
        if not 1024 <= args.port <= 65535:
            parser.error('--port 必须为1024～65535')
        run(args)
    else:
        result = {'hardware': hardware(), 'engine_ready': bool(engine_ready())}
        if args.model:
            result['model'] = model_check(args.model)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止本次启动的模型。', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('错误：' + str(exc), file=sys.stderr)
        sys.exit(1)
