#!/usr/bin/env python3
"""
Kali Security Tools MCP Server
Runs on a Kali Linux box, controlled from Cursor via SSH (stdio transport).

Requires: pip3 install mcp   (FastMCP ships inside the `mcp` package)
Kali tools used: nmap, whois, dig, searchsploit (all preinstalled/available via apt)

IMPORTANT: Only use against systems you are authorized to test.
"""

import re
import shutil
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kali-security")

DEFAULT_TIMEOUT = 120  # seconds


def _run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command on Kali and return stdout+stderr."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        return out if out else (err if err else "(no output)")
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except FileNotFoundError:
        return f"ERROR: {cmd[0]} not found — install it with apt"


def _check_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _validate_target(target: str) -> str | None:
    """Basic sanity check for a hostname or IP. Returns error message or None."""
    target = target.strip()
    if not target or len(target) > 253:
        return "ERROR: empty or too-long target"
    # Allow hostnames, IPv4, IPv6 (loose), and CIDR
    pattern = r"^[A-Za-z0-9._:/-]+$"
    if not re.match(pattern, target):
        return "ERROR: target contains invalid characters"
    return None


@mcp.tool()
def nmap_scan(target: str, ports: str = "--top-ports 100", extra_args: str = "-sV -sT") -> str:
    """Run an nmap scan against a target.

    Args:
        target: IP, hostname, or CIDR range you are authorized to scan.
        ports: port spec, e.g. '--top-ports 100', '80,443', '1-1000'.
        extra_args: extra nmap flags. Defaults to service version detection
                    with a TCP connect scan (no root needed). Add '-sS' for
                    SYN scan (requires root on Kali).
    """
    err = _validate_target(target)
    if err:
        return err
    if not _check_binary("nmap"):
        return "ERROR: nmap not installed (sudo apt install nmap)"
    cmd = ["nmap"] + extra_args.split() + ports.split() + [target]
    return _run(cmd, timeout=300)


@mcp.tool()
def whois_lookup(domain: str) -> str:
    """Run a whois lookup on a domain."""
    err = _validate_target(domain)
    if err:
        return err
    if not _check_binary("whois"):
        return "ERROR: whois not installed (sudo apt install whois)"
    return _run(["whois", domain], timeout=60)


@mcp.tool()
def dns_lookup(domain: str, record_type: str = "A") -> str:
    """DNS lookup via dig.

    Args:
        domain: the domain to query.
        record_type: A, AAAA, MX, NS, TXT, SOA, CNAME, ANY...
    """
    err = _validate_target(domain)
    if err:
        return err
    if not _check_binary("dig"):
        return "ERROR: dig not installed (sudo apt install dnsutils)"
    return _run(["dig", "+noall", "+answer", domain, record_type], timeout=60)


@mcp.tool()
def searchsploit_search(query: str) -> str:
    """Search local Exploit-DB database with searchsploit."""
    if not _check_binary("searchsploit"):
        return "ERROR: searchsploit not installed (sudo apt install exploitdb)"
    return _run(["searchsploit", "--color"] + query.split(), timeout=60)


@mcp.tool()
def run_command(command: str, timeout: int = 60) -> str:
    """Run an arbitrary shell command on the Kali box and return the output.

    Use this for tools not wrapped above (nikto, gobuster, theHarvester, etc.).
    WARNING: this gives the agent full shell access to Kali — use with care.
    """
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    return out if out else (err if err else "(no output)")


@mcp.tool()
def kali_info() -> str:
    """Quick overview of the Kali box: OS, hostname, and which security tools are available."""
    tools = ["nmap", "whois", "dig", "searchsploit", "nikto", "gobuster",
             "sqlmap", "metasploit", "hydra", "theHarvester"]
    available = {t: bool(_check_binary(t)) for t in tools}
    lines = [_run(["uname", "-a"])]
    lines.append(_run(["bash", "-c", "cat /etc/os-release | head -2"]))
    lines.append("Available tools:")
    lines += [f"  {t}: {'yes' if ok else 'no'}" for t, ok in available.items()]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()