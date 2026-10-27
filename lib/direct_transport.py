"""Direct-only macOS transport; adapted from the verified project downloader."""
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.parse

MARKER = "CODER390_METRICS:"
DIRECT_INTERFACE = None

def configure(interface=None):
    global DIRECT_INTERFACE
    if interface is None:
        r = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=3)
        match = re.search(r"interface:\s+(en[0-9]+)\b", r.stdout)
        if r.returncode or not match:
            raise RuntimeError("无法确定物理直连网卡，请用 --interface en0（按实际网卡填写）；不切换代理")
        interface = match.group(1)
    if not re.fullmatch(r"en[0-9]+", interface):
        raise ValueError("--interface 必须是物理网卡 en0/en1/...，不接受隧道网卡")
    DIRECT_INTERFACE = interface
    return interface

class DirectRouteError(RuntimeError):
    """Stop rather than guessing whether an unverified connection is direct."""

def direct_environment():
    return {k: v for k, v in os.environ.items()
            if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}

def curl_direct_args(endpoint):
    args = ["/usr/bin/curl", "-q", "--noproxy", "*", "--proxy", "",
            "--interface", DIRECT_INTERFACE, "--ipv4", "--fail",
            "--proto", "=https", "--proto-redir", "=https",
            "--resolve", "%s:%d:%s" % (endpoint['host'], endpoint['port'], endpoint['ip']),
            "--connect-timeout", "15", "--retry", "0", "--silent", "--show-error"]
    if endpoint["host"] == "api.github.com" and "/releases/assets/" in urllib.parse.urlsplit(endpoint["url"]).path:
        args += ["--header", "Accept: application/octet-stream"]
    return args

def interface_ipv4():
    try:
        result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", DIRECT_INTERFACE],
                                capture_output=True, text=True, timeout=3)
        address = ipaddress.IPv4Address(result.stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise DirectRouteError("无法确认 所选物理网卡 的有效IPv4地址，停止直连") from exc
    if result.returncode or address.is_unspecified or address.is_loopback or address.is_link_local:
        raise DirectRouteError("所选物理网卡 没有可用IPv4地址，停止直连")
    return str(address)

def direct_route(address):
    try:
        peer = ipaddress.IPv4Address(address)
    except ValueError as exc:
        raise DirectRouteError("目标不是有效IPv4地址") from exc
    if not peer.is_global:
        raise DirectRouteError("目标不是公网IPv4，拒绝代理/Fake-IP地址")
    try:
        result = subprocess.run(["/sbin/route", "-n", "get", str(peer)],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DirectRouteError("无法查询目标路由，停止直连") from exc
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in ("interface", "gateway"):
            values[key] = value.strip()
    if result.returncode or values.get("interface") != DIRECT_INTERFACE:
        raise DirectRouteError("目标路由未确认走 所选物理网卡，停止；不会切换到代理")
    return values

def resolve_direct_endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise DirectRouteError("仅允许无用户信息的HTTPS下载地址")
    try:
        host = parsed.hostname.encode('idna').decode('ascii')
        port = parsed.port or 443
        local_ip = interface_ipv4()
        answers = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except (ValueError, UnicodeError, OSError) as exc:
        raise DirectRouteError("无法解析下载目标的IPv4地址") from exc
    ips = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not ips:
        raise DirectRouteError("下载目标没有IPv4地址")
    routes = {ip: direct_route(ip) for ip in ips}
    return {'url': url, 'host': host, 'port': port, 'ip': ips[0],
            'local_ip': local_ip, 'route': routes[ips[0]], 'checked_ips': ips}

def curl_metrics(stdout):
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except json.JSONDecodeError as exc:
                raise DirectRouteError("curl连接回执无法解析") from exc
    raise DirectRouteError("缺少curl连接回执，无法确认直连")

def check_transfer_endpoint(metrics, endpoint):
    if metrics.get('remote_ip') != endpoint['ip'] or metrics.get('local_ip') != endpoint['local_ip']:
        raise DirectRouteError("curl实际连接与已核对的所选物理网卡地址不符")
    if interface_ipv4() != endpoint['local_ip']:
        raise DirectRouteError("所选物理网卡地址在传输期间变化，已停止")
    return direct_route(endpoint['ip'])

def response_headers(stdout):
    headers = {}
    for line in stdout.splitlines():
        if line.startswith('HTTP/'):
            headers = {}
        elif not line.startswith(MARKER):
            key, sep, value = line.partition(':')
            if sep:
                headers[key.strip().lower()] = value.strip()
    return headers

def preflight_direct(url, head_request, check_pause=lambda: None):
    """Resolve/route-check every hop BEFORE its HEAD. Never follow redirects implicitly."""
    seen = set()
    hops = []
    for _ in range(6):
        check_pause()
        if url in seen:
            raise DirectRouteError("下载地址存在重定向循环")
        seen.add(url)
        endpoint = resolve_direct_endpoint(url)
        code, stdout, _stderr = head_request(endpoint)
        if code:
            raise DirectRouteError("直连HEAD预检失败，curl=%s；不会切换代理" % code)
        metrics = curl_metrics(stdout)
        endpoint['route'] = check_transfer_endpoint(metrics, endpoint)
        status = int(metrics.get('http_code') or 0)
        hops.append({'host': endpoint['host'], 'ip': endpoint['ip'],
                     'local_ip': endpoint['local_ip'], 'http_code': status,
                     'checked_ips': endpoint['checked_ips'], 'route': endpoint['route']})
        if status in (301, 302, 303, 307, 308):
            location = response_headers(stdout).get('location')
            if not location:
                raise DirectRouteError("HEAD重定向没有Location，停止")
            url = urllib.parse.urljoin(url, location)
            continue
        if not 200 <= status < 300:
            raise DirectRouteError("HEAD返回HTTP %d，停止" % status)
        endpoint['hops'] = hops
        return endpoint
    raise DirectRouteError("HEAD重定向超过5次，停止")

def terminate_and_wait(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    else:
        process.wait()
