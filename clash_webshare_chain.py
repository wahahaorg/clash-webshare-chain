#!/usr/bin/env python3
"""Probe Clash Verge airport exits through a Webshare static-residential chain.

The chain is  airport exit -> Webshare static residential -> target.  Webshare
only accepts connections from specific airport exit IPs: every other exit
completes the TCP handshake and then stalls on the authenticated CONNECT
(observed as a ~5s hang followed by a TLS error).  A plain TCP check therefore
over-reports working exits, so this tool probes each node end-to-end through
the local Clash proxy and only keeps the ones that answer.

`run` does the whole cycle:
  1. render Script.js with every candidate node in the upstream group
  2. restart Clash Verge and probe each node through the real chain
  3. render Script.js with only the verified exits, fastest first
  4. restart Clash Verge, verify the top exit, and report

Only stdlib is used.  Tested on macOS with Clash Verge (service mode).
"""

from __future__ import annotations

import argparse
import glob
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "clash-webshare-chain" / "config.json"

GROUP_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Direct",
    "Reject",
    "RejectDrop",
    "Compatible",
    "Pass",
    "PassRule",
    "Dns",
}

DEFAULTS = {
    "mihomo_socket": "/var/run/clash-verge-service/users/501/verge-mihomo.sock",
    "mihomo_url": None,
    "mihomo_secret": "set-your-secret",
    "clash_proxy": "http://127.0.0.1:7897",
    "script_path": "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/Script.js",
    "profiles_yaml": "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles.yaml",
    "profile_name": None,
    "upstream_group": "Webshare · 上游",
    "antigravity_group": "Antigravity · Residential Chain",
    "residential_proxy": "Webshare · Static Residential",
    "legacy_groups": ["Webshare · 上游(美国)"],
    "webshare": {
        "server": "YOUR-WEBSHARE-ENDPOINT",
        "port": 7208,
        "username": "",
        "password": "",
    },
    "probe_url": "https://generativelanguage.googleapis.com/v1beta/models",
    "success_status": [200, 401, 403, 404],
    "timeout": 10.0,
    "settle": 0.4,
    "attempts": 2,
    "exclude_patterns": [
        "剩余流量",
        "距离下次重置",
        "套餐到期",
        "官网",
        "客户端",
        "无法使用",
        "去广告",
        "^若",
    ],
    "quit_cmd": 'osascript -e \'quit app "Clash Verge"\'',
    "start_cmd": 'open -a "Clash Verge"',
    "process_pattern": "/Applications/Clash Verge.app/Contents/MacOS/clash-verge",
    "restart_timeout": 120,
}


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config_path(args) -> Path:
    raw = args.config or os.environ.get("CWC_CONFIG")
    return Path(os.path.expanduser(raw)) if raw else DEFAULT_CONFIG_PATH


def load_config(args) -> tuple[dict, Path]:
    path = config_path(args)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n")
        raise SystemExit(
            f"created {path}\n"
            "fill in webshare.server/username/password (and the paths if they differ), "
            "then re-run."
        )
    cfg = deep_merge(DEFAULTS, json.loads(path.read_text()))

    # macOS service mode defaults to a per-uid unix socket.
    sock = os.path.expanduser(str(cfg.get("mihomo_socket") or ""))
    if sock and not os.path.exists(sock):
        found = sorted(glob.glob("/var/run/clash-verge-service/users/*/verge-mihomo.sock"))
        if len(found) == 1:
            cfg["mihomo_socket"] = found[0]
        elif not cfg.get("mihomo_url"):
            print(f"warning: mihomo socket not found: {sock}", file=sys.stderr)
    return cfg, path


def load_state(path: Path) -> dict:
    state_path = path.parent / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"profiles": {}}


def save_state(path: Path, state: dict) -> None:
    (path.parent / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    )


def detect_active_profile(profiles_yaml: str) -> str | None:
    path = Path(os.path.expanduser(profiles_yaml))
    if not path.exists():
        return None
    text = path.read_text()
    current = re.search(r"^current:\s*(\S+)\s*$", text, re.M)
    if not current:
        return None
    uid = current.group(1)
    for block in re.split(r"^- uid:\s*", text, flags=re.M)[1:]:
        lines = block.splitlines()
        if lines and lines[0].strip() == uid:
            match = re.search(r"^\s+name:\s*(.+?)\s*$", "\n".join(lines[1:]), re.M)
            if match:
                name = match.group(1).strip()
                if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
                    name = name[1:-1]
                return name
    return None


# --------------------------------------------------------------------------
# mihomo API (unix socket or tcp)
# --------------------------------------------------------------------------


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class Mihomo:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.secret = cfg.get("mihomo_secret") or ""
        url = cfg.get("mihomo_url")
        if url:
            parsed = urllib.parse.urlparse(url)
            self.mode = "tcp"
            self.host = parsed.hostname
            self.port = parsed.port or 9090
            self.base = parsed.path.rstrip("/")
        else:
            self.mode = "unix"
            self.socket_path = os.path.expanduser(cfg["mihomo_socket"])
            self.base = ""

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.mode == "unix":
            return UnixHTTPConnection(self.socket_path, timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def request(self, method: str, path: str, body=None, timeout: float = 10.0):
        conn = self._connection(timeout)
        headers = {}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, self.base + path, payload, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, raw

    def proxies(self) -> dict:
        status, raw = self.request("GET", "/proxies")
        if status != 200:
            raise RuntimeError(f"GET /proxies -> HTTP {status}")
        return json.loads(raw)["proxies"]

    def group(self, name: str) -> dict | None:
        return self.proxies().get(name)

    def select(self, group: str, node: str) -> int:
        status, _ = self.request(
            "PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": node}
        )
        return status

    def wait_for_group(
        self, group: str, expected: int | None, timeout: float
    ) -> dict:
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                proxies = self.proxies()
                info = proxies.get(group)
                if info and (expected is None or len(info.get("all", [])) == expected):
                    return proxies
                if info:
                    last_error = (
                        f"group has {len(info.get('all', []))} options, "
                        f"expected {expected} (config not reloaded yet)"
                    )
            except Exception as exc:  # noqa: BLE001 - core is restarting
                last_error = str(exc)
            time.sleep(1)
        raise RuntimeError(f"timed out waiting for group {group!r}: {last_error}")


# --------------------------------------------------------------------------
# nodes / probe / restart
# --------------------------------------------------------------------------


def leaf_nodes(proxies: dict, cfg: dict) -> list[str]:
    exclude = [re.compile(p) for p in cfg["exclude_patterns"]]
    reserved = {cfg["residential_proxy"], "DIRECT", "REJECT", "GLOBAL"}
    nodes = []
    for name, info in proxies.items():
        if info.get("type") in GROUP_TYPES:
            continue
        if name in reserved:
            continue
        if any(pattern.search(name) for pattern in exclude):
            continue
        nodes.append(name)
    return nodes


def is_success(code: str, cfg: dict) -> bool:
    return code.isdigit() and int(code) in cfg["success_status"]


def probe(cfg: dict, timeout: float | None = None) -> tuple[str, float, str]:
    command = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-m",
        str(timeout if timeout is not None else cfg["timeout"]),
    ]
    if cfg.get("clash_proxy"):
        command += ["-x", cfg["clash_proxy"]]
    command.append(cfg["probe_url"])
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True)
    elapsed = round(time.monotonic() - started, 2)
    code = completed.stdout.strip()
    if not code:
        code = "000"
    return code, elapsed, completed.stderr.strip()


def sweep(api: Mihomo, nodes: list[str], cfg: dict) -> dict:
    results: dict[str, dict] = {}
    attempts = max(1, int(cfg["attempts"]))
    for attempt in range(1, attempts + 1):
        todo = [name for name in nodes if not results.get(name, {}).get("ok")]
        if not todo:
            break
        print(f"\n=== probe round {attempt}/{attempts}: {len(todo)} nodes ===")
        for index, name in enumerate(todo, 1):
            status = api.select(cfg["upstream_group"], name)
            if status not in (200, 204):
                reason = f"selector rejected (HTTP {status}); node not in group"
                print(f"[{index:>2}/{len(todo)}] SKIP  {name}  ({reason})")
                results[name] = {
                    "ok": False,
                    "code": None,
                    "latency": None,
                    "error": reason,
                }
                continue
            time.sleep(float(cfg["settle"]))
            code, elapsed, error = probe(cfg)
            ok = is_success(code, cfg)
            if ok:
                results[name] = {
                    "ok": True,
                    "code": code,
                    "latency": elapsed,
                    "error": "",
                }
            elif not results.get(name, {}).get("ok"):
                results[name] = {
                    "ok": False,
                    "code": code,
                    "latency": elapsed,
                    "error": error,
                }
            label = "OK  " if ok else "FAIL"
            print(f"[{index:>2}/{len(todo)}] {label}  {name}  {code}  {elapsed}s")
    return results


def wait_process_gone(pattern: str, timeout: float = 20.0) -> bool:
    if not pattern:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        exists = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        if exists.returncode != 0:
            return True
        time.sleep(0.5)
    return False


def restart(api: Mihomo, cfg: dict, expected: int | None) -> None:
    print("restarting Clash Verge ...")
    if cfg.get("quit_cmd"):
        subprocess.run(cfg["quit_cmd"], shell=True, check=False)
    if not wait_process_gone(cfg.get("process_pattern", "")):
        raise RuntimeError(
            "Clash Verge did not exit; refusing to continue with a stale config"
        )
    time.sleep(1)
    if cfg.get("start_cmd"):
        subprocess.run(cfg["start_cmd"], shell=True, check=False)
    api.wait_for_group(
        cfg["upstream_group"], expected, float(cfg["restart_timeout"])
    )
    suffix = f"has {expected} options" if expected is not None else "is ready"
    print(f"  ready: {cfg['upstream_group']} {suffix}")


# --------------------------------------------------------------------------
# Script.js rendering
# --------------------------------------------------------------------------


def backup_script(script: Path) -> Path | None:
    if not script.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = script.with_name(f"{script.name}.bak-{stamp}")
    shutil.copy2(script, backup)
    backups = sorted(script.parent.glob(f"{script.name}.bak-*"))
    for old in backups[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


def render_script(
    cfg: dict,
    path: Path,
    state: dict,
    profile: str,
    override: list[str] | None = None,
    out: Path | None = None,
) -> Path:
    template = (PROJECT_DIR / "script_template.js").read_text()
    profile_nodes = dict(state.get("profiles", {}))
    if override is not None:
        profile_nodes[profile] = list(override)

    webshare = cfg["webshare"]
    js = template
    js = js.replace('"__SERVER__"', json.dumps(webshare["server"], ensure_ascii=False))
    js = js.replace("__PORT__", str(int(webshare["port"])))
    js = js.replace('"__USERNAME__"', json.dumps(webshare["username"], ensure_ascii=False))
    js = js.replace('"__PASSWORD__"', json.dumps(webshare["password"], ensure_ascii=False))
    js = js.replace(
        "__PROFILE_NODES__", json.dumps(profile_nodes, ensure_ascii=False, indent=2)
    )

    destination = Path(out) if out else path
    if destination == path:
        backup_script(path)
    tmp = destination.with_name(destination.name + ".tmp.js")
    tmp.write_text(js)

    node = shutil.which("node")
    if node:
        check = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
        if check.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"generated Script.js failed node --check:\n{check.stderr}")
    os.replace(tmp, destination)
    os.chmod(destination, 0o600)
    return destination


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_nodes(args, cfg, _path):
    api = Mihomo(cfg)
    proxies = api.proxies()
    groups = [
        (name, len(info.get("all", [])))
        for name, info in proxies.items()
        if info.get("type") in GROUP_TYPES and info.get("all") is not None
    ]
    nodes = leaf_nodes(proxies, cfg)
    if args.json:
        print(json.dumps({"groups": groups, "nodes": nodes}, ensure_ascii=False, indent=2))
        return
    print(f"groups ({len(groups)}):")
    for name, count in groups:
        print(f"  {name}  ({count} options)")
    print(f"\nleaf nodes ({len(nodes)}):")
    for name in nodes:
        print("  " + name)


def cmd_probe(args, cfg, _path):
    api = Mihomo(cfg)
    group = api.group(cfg["upstream_group"])
    if not group:
        raise SystemExit(
            f"group {cfg['upstream_group']!r} not found; run `run` or fix config"
        )
    nodes = group["all"]
    if args.limit:
        nodes = nodes[: args.limit]
    print(f"probing {len(nodes)} options of {cfg['upstream_group']}")
    results = sweep(api, nodes, cfg)
    verified = sorted(
        (name for name, info in results.items() if info["ok"]),
        key=lambda name: results[name]["latency"],
    )
    print(f"\nverified {len(verified)}/{len(results)}:")
    for name in verified:
        print(f"  {results[name]['latency']:>5}s  {name}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"group": cfg["upstream_group"], "results": results, "verified": verified},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {args.json}")


def cmd_render(args, cfg, path):
    state = load_state(path)
    profile = args.profile or cfg.get("profile_name") or detect_active_profile(
        cfg["profiles_yaml"]
    )
    if not profile:
        raise SystemExit("cannot determine profile name; pass --profile")
    out = Path(args.out) if args.out else Path(os.path.expanduser(cfg["script_path"]))
    render_script(cfg, Path(os.path.expanduser(cfg["script_path"])), state, profile, out=out)
    print(f"rendered {out} for profile {profile!r}")


def cmd_run(args, cfg, path):
    script = Path(os.path.expanduser(cfg["script_path"]))
    profile = args.profile or cfg.get("profile_name") or detect_active_profile(
        cfg["profiles_yaml"]
    )
    if not profile:
        raise SystemExit("cannot determine profile name; pass --profile")
    webshare = cfg["webshare"]
    missing = [
        key
        for key in ("server", "username", "password")
        if not webshare.get(key) or str(webshare.get(key)).startswith("YOUR-")
    ]
    if missing:
        raise SystemExit(
            f"missing webshare credentials in {path}: {', '.join(missing)}"
        )

    api = Mihomo(cfg)
    candidates = leaf_nodes(api.proxies(), cfg)
    print(f"profile: {profile}")
    print(f"candidate exits: {len(candidates)}")
    if not candidates:
        raise SystemExit("no leaf nodes found")

    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("refusing to rewrite Script.js without --yes")
        answer = input(
            f"rewrite {script} and restart Clash Verge twice (first with "
            f"{len(candidates)} candidates, then with verified only)? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("aborted")

    state = load_state(path)
    original = script.read_text() if script.exists() else None

    try:
        print("\n--- phase 1: candidate config ---")
        render_script(cfg, script, state, profile, override=candidates)
        restart(api, cfg, expected=len(candidates))

        results = sweep(api, candidates, cfg)
        verified = sorted(
            (name for name, info in results.items() if info["ok"]),
            key=lambda name: results[name]["latency"],
        )
        if not verified:
            raise RuntimeError("no exit passed the end-to-end probe")

        state.setdefault("profiles", {})[profile] = verified
        save_state(path, state)

        # Clash Verge persists the live selection on quit; make it a good one
        # before the final restart so a dead probe node is never restored.
        api.select(cfg["upstream_group"], verified[0])

        print("\n--- phase 2: verified config ---")
        render_script(cfg, script, state, profile)
        restart(api, cfg, expected=len(verified))

        chosen = None
        for name in verified[: min(3, len(verified))]:
            api.select(cfg["upstream_group"], name)
            time.sleep(float(cfg["settle"]))
            code, elapsed, _ = probe(cfg)
            ok = is_success(code, cfg)
            print(f"verify {'OK  ' if ok else 'FAIL'}  {name}  {code}  {elapsed}s")
            if ok:
                chosen = name
                break

        if chosen is None:
            print("warning: top candidates failed re-verification; keeping the list")
        elif chosen != verified[0]:
            verified.remove(chosen)
            verified.insert(0, chosen)
            state["profiles"][profile] = verified
            save_state(path, state)
        api.select(cfg["upstream_group"], chosen or verified[0])

        timestamp = datetime.now().isoformat(timespec="seconds")
        report = {
            "timestamp": timestamp,
            "profile": profile,
            "probe_url": cfg["probe_url"],
            "group": cfg["upstream_group"],
            "verified": verified,
            "results": results,
        }
        report_path = path.parent / "last-run.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        print(f"\nverified {len(verified)}/{len(candidates)} exits:")
        for name in verified:
            print(f"  {results[name]['latency']:>5}s  {name}")
        print(f"\nstate: {path.parent / 'state.json'}")
        print(f"report: {report_path}")
    except Exception:
        if original is not None:
            print("\nrestoring previous Script.js ...", file=sys.stderr)
            script.write_text(original)
            try:
                restart(api, cfg, expected=None)
            except Exception as exc:  # noqa: BLE001 - best-effort restore
                print(f"warning: restore restart failed: {exc}", file=sys.stderr)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe Clash Verge exits through a Webshare chain and regenerate Script.js."
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--profile", help="profile name passed to Script.js")
    sub = parser.add_subparsers(dest="command", required=True)

    nodes = sub.add_parser("nodes", help="list groups and leaf nodes")
    nodes.add_argument("--json", action="store_true")
    nodes.set_defaults(func=cmd_nodes)

    probe = sub.add_parser("probe", help="probe the current upstream group")
    probe.add_argument("--limit", type=int)
    probe.add_argument("--json", help="write results to this JSON file")
    probe.set_defaults(func=cmd_probe)

    render = sub.add_parser("render", help="render Script.js from saved state only")
    render.add_argument("--out", help="write here instead of script_path")
    render.set_defaults(func=cmd_render)

    run = sub.add_parser("run", help="full cycle: candidates -> probe -> verified")
    run.add_argument("--yes", action="store_true", help="skip confirmation")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    cfg, path = load_config(args)
    args.func(args, cfg, path)


if __name__ == "__main__":
    main()
