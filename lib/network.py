"""Pinned, resumable tool/source downloads; no model downloads."""
import json
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from .util import digest, write
from . import direct_transport

def framework():
    if direct_transport.DIRECT_INTERFACE is None:
        direct_transport.configure()
    return direct_transport

def check_process(direct, process, endpoint):
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(process.pid), '-iTCP'],
                            capture_output=True, text=True, timeout=4)
    connections = [line.split('TCP ', 1)[1].split(' ', 1)[0]
                   for line in result.stdout.splitlines() if 'TCP ' in line and '->' in line]
    if not connections:
        if process.poll() is not None:
            return
        raise direct.DirectRouteError('Cannot verify the active download connection')
    for connection in connections:
        local, remote = connection.split('->', 1)
        direct.check_transfer_endpoint({'local_ip': local.rsplit(':', 1)[0],
                                        'remote_ip': remote.rsplit(':', 1)[0]}, endpoint)

def fetch(url, destination, expected_sha=None, expected_size=None, limit=256 << 20):
    """No implicit redirects, proxy fallback, pip downloads or global settings."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if expected_sha and destination.is_file() and digest(destination) == expected_sha:
        if expected_size is None or destination.stat().st_size == expected_size:
            return {'path': str(destination), 'sha256': expected_sha, 'size': destination.stat().st_size, 'reused': True}
    if destination.exists():
        raise RuntimeError('Existing unverified artifact is not overwritten: ' + str(destination))
    direct = framework()
    partial = destination.with_name(destination.name + '.part')
    if expected_sha and expected_size is not None and partial.is_file() and partial.stat().st_size == expected_size:
        if digest(partial) != expected_sha:
            raise RuntimeError('Complete partial artifact has the wrong SHA-256; retained for inspection')
        partial.replace(destination)
        receipt = {'path': str(destination), 'sha256': expected_sha, 'size': expected_size,
                   'reused': True, 'recovered_complete_partial': True,
                   'transport_observed_this_run': False, 'expected_sha256_verified': True}
        write(destination.with_name(destination.name + '.download.json'), receipt)
        return receipt
    maximum = expected_size if expected_size is not None else limit
    initial_url = url
    attempts = redirects = 0
    hops = []
    def head(endpoint):
        result = subprocess.run(direct.curl_direct_args(endpoint) + [
            '--head', '--max-time', '30', '--write-out', '\n' + direct.MARKER + '%{json}\n', endpoint['url']],
            capture_output=True, text=True, env=direct.direct_environment(), timeout=35)
        return result.returncode, result.stdout, result.stderr
    while attempts < 6:
        endpoint = direct.preflight_direct(url, head)
        hops.extend(endpoint['hops'])
        before = partial.stat().st_size if partial.exists() else 0
        if before > maximum:
            raise RuntimeError('Partial file exceeds declared artifact size')
        with tempfile.TemporaryFile(mode='w+t') as out, tempfile.TemporaryFile(mode='w+t') as err, tempfile.NamedTemporaryFile(mode='w+t') as headers_file:
            command = direct.curl_direct_args(endpoint) + [
                '--continue-at', '-', '--max-redirs', '0', '--max-filesize', str(max(maximum, 1 << 20)),
                '--speed-limit', '1024', '--speed-time', '90', '--dump-header', headers_file.name,
                '--output', str(partial), '--write-out', '\n' + direct.MARKER + '%{json}\n', endpoint['url']]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=direct.direct_environment(), start_new_session=True)
            started = last_check = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > 7200:
                        raise TimeoutError('Artifact transfer exceeded two hours')
                    if time.monotonic() - last_check >= 5:
                        check_process(direct, process, endpoint)
                        last_check = time.monotonic()
                    time.sleep(.25)
            finally:
                direct.terminate_and_wait(process)
            out.seek(0); err.seek(0); headers_file.seek(0)
            stdout, stderr = out.read(), err.read()
            headers = direct.response_headers(headers_file.read())
        metrics = direct.curl_metrics(stdout)
        direct.check_transfer_endpoint(metrics, endpoint)
        status = int(metrics.get('http_code') or 0)
        if status in (301, 302, 303, 307, 308):
            if partial.exists():
                with partial.open('r+b') as f:
                    f.truncate(before)
            location = headers.get('location')
            redirects += 1
            if not location or redirects > 5:
                raise direct.DirectRouteError('Body redirect could not be verified')
            url = urllib.parse.urljoin(endpoint['url'], location)
            continue
        actual = partial.stat().st_size if partial.exists() else 0
        if actual > maximum:
            raise RuntimeError('Artifact exceeded its declared size limit')
        if process.returncode == 0 and status in (200, 206):
            if expected_size is not None and actual != expected_size:
                raise RuntimeError('Artifact length mismatch')
            sha = digest(partial)
            if expected_sha is not None and sha != expected_sha:
                raise RuntimeError('Artifact SHA-256 mismatch; partial retained for inspection')
            partial.replace(destination)
            receipt = {'path': str(destination), 'sha256': sha, 'size': actual, 'interface': 'en2',
                       'source_host': urllib.parse.urlsplit(initial_url).hostname,
                       'final_host': endpoint['host'], 'hops': hops, 'reused': False,
                       'expected_sha256_verified': expected_sha is not None}
            write(destination.with_name(destination.name + '.download.json'), receipt)
            return receipt
        attempts += 1
        # Re-resolve expiring redirects from the original immutable artifact URL.
        url = initial_url
        redirects = 0
        error = re.sub(r'https?://\S+', '[URL]', stderr.strip())[:160]
        if attempts == 6:
            raise RuntimeError('Direct download failed; no proxy fallback: ' + error)
        time.sleep(min(30, attempts * 5))
    raise RuntimeError('Artifact transfer did not complete')
