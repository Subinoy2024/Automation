#!/usr/bin/env python3
"""
DevSecOps Tools Installer (Ubuntu amd64) — idempotent + status mode

Features
- Re-run safe: shows STATUS + versions, skips already-installed tools
- --check-only : audit mode (no changes)
- --force      : re-install (or re-run install steps) even if detected installed
- Logs to /var/log/devsecops-install.log (or ./devsecops-install.log in check-only non-root)

Usage
  sudo python3 devsecops_install.py
  sudo python3 devsecops_install.py --force
  python3 devsecops_install.py --check-only
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# -------------------------
# Args / Modes
# -------------------------
FORCE_INSTALL = "--force" in sys.argv
CHECK_ONLY = "--check-only" in sys.argv

# If check-only and not root, log locally
DEFAULT_LOG = "/var/log/devsecops-install.log"
LOCAL_LOG = str(Path.cwd() / "devsecops-install.log")
LOG_FILE = DEFAULT_LOG

# -------------------------
# Logging + Runner
# -------------------------
def log(msg: str) -> None:
    print(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except PermissionError:
        # Fallback for non-root check-only runs
        with open(LOCAL_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

def run_capture(cmd: str):
    """Runs a shell command and returns (returncode, stdout, stderr)."""
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")

def step(title: str, cmd: str, allow_fail: bool = False) -> bool:
    log(f"\n{title} - START")
    if CHECK_ONLY:
        log(f"{title} - SKIPPED (check-only)")
        log(f"CMD: {cmd}")
        return True

    rc, out, err = run_capture(cmd)
    if out.strip():
        log(out.rstrip())
    if err.strip():
        log(err.rstrip())

    if rc == 0 or allow_fail:
        log(f"{title} - SUCCESS" if rc == 0 else f"{title} - CONTINUED (non-fatal)")
        return True

    log(f"{title} - FAILED (rc={rc})")
    sys.exit(rc)

def require_root_unless_check_only():
    if CHECK_ONLY:
        return
    if os.geteuid() != 0:
        print("Run with sudo:")
        print(f"sudo python3 {Path(sys.argv[0]).name}")
        sys.exit(1)

def get_system_info():
    arch = subprocess.check_output("dpkg --print-architecture", shell=True, text=True).strip()
    codename = subprocess.check_output("lsb_release -cs", shell=True, text=True).strip()
    version = subprocess.check_output("lsb_release -rs", shell=True, text=True).strip()
    return arch, codename, version

# -------------------------
# Detection helpers
# -------------------------
def cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def get_version(version_cmd: str) -> str:
    try:
        out = subprocess.check_output(version_cmd, shell=True, text=True, stderr=subprocess.STDOUT)
        out = out.strip()
        return out.splitlines()[0] if out else "Unknown"
    except Exception:
        return "Unknown"

def dpkg_installed(pkg: str) -> bool:
    rc, _, _ = run_capture(f"dpkg -s {pkg} >/dev/null 2>&1")
    return rc == 0

def file_exists(path: str) -> bool:
    return Path(path).exists()

def ensure_line_in_file(path: str, line: str) -> bool:
    """Idempotently ensure line exists in a file. Returns True if changed."""
    p = Path(path)
    if CHECK_ONLY:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        content = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line in content:
            return False
        content.append(line)
        p.write_text("\n".join(content) + "\n", encoding="utf-8")
        return True
    else:
        p.write_text(line + "\n", encoding="utf-8")
        return True

# -------------------------
# Apt update de-dup
# -------------------------
APT_UPDATED = False

def apt_update_once():
    global APT_UPDATED
    if APT_UPDATED:
        return
    step("apt update", "apt update -y")
    APT_UPDATED = True

# -------------------------
# Install wrappers
# -------------------------
def status_ok(title: str, version: str = ""):
    if version:
        log(f"{title} - INSTALLED | {version}")
    else:
        log(f"{title} - INSTALLED")

def status_missing(title: str):
    log(f"{title} - NOT INSTALLED")

def install_cmd_tool(title: str, check_cmd: str, version_cmd: str, install_cmd: str, allow_fail: bool = False):
    """
    check_cmd: executable name (e.g., 'terraform')
    """
    if cmd_exists(check_cmd) and not FORCE_INSTALL:
        status_ok(title, get_version(version_cmd))
        return

    if cmd_exists(check_cmd) and FORCE_INSTALL:
        log(f"{title} - REINSTALL (forced)")
    else:
        status_missing(title)

    step(title, install_cmd, allow_fail=allow_fail)

    # Post status
    if cmd_exists(check_cmd):
        status_ok(title, get_version(version_cmd))
    else:
        log(f"{title} - STILL MISSING after install attempt")

def install_apt_packages(title: str, packages: list[str], allow_fail: bool = False):
    missing = [p for p in packages if not dpkg_installed(p)]
    if not missing and not FORCE_INSTALL:
        status_ok(title, "All packages present")
        return

    if missing:
        log(f"{title} - MISSING packages: {', '.join(missing)}")
    else:
        log(f"{title} - REINSTALL (forced)")

    apt_update_once()
    step(title, "DEBIAN_FRONTEND=noninteractive apt install -y " + " ".join(packages), allow_fail=allow_fail)

def install_pipx_app(title: str, app_cmd: str, pipx_pkg: str, version_cmd: str):
    """
    Installs a Python CLI via pipx (isolated). Works best when script run as root; we symlink to /usr/local/bin.
    """
    if cmd_exists(app_cmd) and not FORCE_INSTALL:
        status_ok(title, get_version(version_cmd))
        return

    if cmd_exists(app_cmd) and FORCE_INSTALL:
        log(f"{title} - REINSTALL (forced)")
    else:
        status_missing(title)

    # Ensure pipx installed (apt package)
    install_apt_packages("pipx", ["pipx"], allow_fail=False)

    # Install/upgrade via pipx
    step(f"{title} (pipx install/upgrade)", f"pipx install {pipx_pkg} || pipx upgrade {pipx_pkg}", allow_fail=False)

    # Expose binary globally (root pipx path)
    # If check-only, this step will be skipped anyway.
    step(f"{title} (symlink /usr/local/bin)", f"ln -sf /root/.local/bin/{app_cmd} /usr/local/bin/{app_cmd} || true", allow_fail=True)

    if cmd_exists(app_cmd):
        status_ok(title, get_version(version_cmd))
    else:
        log(f"{title} - STILL MISSING after pipx attempt")

# -------------------------
# Main
# -------------------------
def main():
    global LOG_FILE

    require_root_unless_check_only()

    # Decide log path
    if CHECK_ONLY and os.geteuid() != 0:
        LOG_FILE = LOCAL_LOG
    else:
        LOG_FILE = DEFAULT_LOG
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    log("\n" + "=" * 70)
    log(f"INSTALL RUN: {datetime.now().isoformat(timespec='seconds')}")
    log(f"MODE: {'CHECK-ONLY' if CHECK_ONLY else 'INSTALL'} | FORCE: {FORCE_INSTALL}")
    log("=" * 70)

    arch, codename, version = get_system_info()
    if arch != "amd64":
        log(f"Only amd64 supported. Detected: {arch}")
        sys.exit(1)

    log(f"Ubuntu {version} ({codename}) | Arch: {arch}")
    sudo_user = os.environ.get("SUDO_USER")  # the user who invoked sudo (if any)

    # -------------------------
    # System update (optional but useful)
    # -------------------------
    if not CHECK_ONLY:
        apt_update_once()
        step("System Upgrade", "DEBIAN_FRONTEND=noninteractive apt upgrade -y", allow_fail=False)

    # -------------------------
    # Base packages
    # -------------------------
    install_apt_packages(
        "Base Packages",
        [
            "curl", "wget", "unzip", "zip", "tar", "git", "jq", "make", "gcc", "g++",
            "python3", "python3-pip", "python3-venv",
            "apt-transport-https", "ca-certificates", "gnupg", "lsb-release", "software-properties-common",
            "openssh-client", "fontconfig"
        ],
        allow_fail=False
    )

    # -------------------------
    # Docker
    # -------------------------
    docker_key = "/etc/apt/keyrings/docker.gpg"
    docker_list = "/etc/apt/sources.list.d/docker.list"

    if file_exists(docker_key) and file_exists(docker_list) and not FORCE_INSTALL:
        status_ok("Docker Repo", "Key + repo present")
    else:
        step(
            "Docker Repo Key",
            "install -m 0755 -d /etc/apt/keyrings && "
            "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | "
            "gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg && "
            "chmod a+r /etc/apt/keyrings/docker.gpg",
            allow_fail=False
        )
        # Write repo line idempotently
        repo_line = f"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu {codename} stable"
        if CHECK_ONLY:
            log(f"Docker Repository - SKIPPED (check-only) would ensure line in {docker_list}: {repo_line}")
        else:
            ensure_line_in_file(docker_list, repo_line)
            log("Docker Repository - ensured")

    # Install docker engine if missing
    if not dpkg_installed("docker-ce") or FORCE_INSTALL:
        apt_update_once()
        step(
            "Install Docker Engine",
            "DEBIAN_FRONTEND=noninteractive apt install -y "
            "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
            allow_fail=False
        )
    # Status
    if cmd_exists("docker"):
        status_ok("Docker", get_version("docker --version"))
    else:
        status_missing("Docker")

    if sudo_user:
        step("Add user to docker group (best effort)", f"usermod -aG docker {sudo_user} || true", allow_fail=True)

    # -------------------------
    # Kubernetes Tools
    # -------------------------
    install_cmd_tool(
        "kubectl",
        "kubectl",
        "kubectl version --client --short",
        "bash -lc 'VER=$(curl -fsSL https://dl.k8s.io/release/stable.txt) && "
        "curl -fsSL -o /usr/local/bin/kubectl "
        "\"https://dl.k8s.io/release/${VER}/bin/linux/amd64/kubectl\" && "
        "chmod +x /usr/local/bin/kubectl'",
        allow_fail=False
    )

    install_cmd_tool(
        "Helm",
        "helm",
        "helm version --short",
        "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash",
        allow_fail=False
    )

    install_cmd_tool(
        "k9s",
        "k9s",
        "k9s version",
        "curl -fsSL https://webinstall.dev/k9s | bash",
        allow_fail=True
    )

    # kubectx/kubens (symlinks to /usr/local/bin)
    if (cmd_exists("kubectx") and cmd_exists("kubens")) and not FORCE_INSTALL:
        status_ok("kubectx/kubens", f"{get_version('kubectx --help')} / {get_version('kubens --help')}")
    else:
        step(
            "Install kubectx/kubens",
            "rm -rf /opt/kubectx || true && "
            "git clone https://github.com/ahmetb/kubectx /opt/kubectx && "
            "ln -sf /opt/kubectx/kubectx /usr/local/bin/kubectx && "
            "ln -sf /opt/kubectx/kubens /usr/local/bin/kubens",
            allow_fail=True
        )
        if cmd_exists("kubectx") and cmd_exists("kubens"):
            status_ok("kubectx/kubens")
        else:
            status_missing("kubectx/kubens")

    # -------------------------
    # Terraform
    # -------------------------
    hashicorp_key = "/etc/apt/keyrings/hashicorp.gpg"
    hashicorp_list = "/etc/apt/sources.list.d/hashicorp.list"

    if file_exists(hashicorp_key) and file_exists(hashicorp_list) and not FORCE_INSTALL:
        status_ok("HashiCorp Repo", "Key + repo present")
    else:
        step(
            "HashiCorp Repo Key",
            "install -m 0755 -d /etc/apt/keyrings && "
            "curl -fsSL https://apt.releases.hashicorp.com/gpg | "
            "gpg --dearmor --yes -o /etc/apt/keyrings/hashicorp.gpg",
            allow_fail=False
        )
        repo_line = f"deb [signed-by=/etc/apt/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com {codename} main"
        if CHECK_ONLY:
            log(f"HashiCorp Repository - SKIPPED (check-only) would ensure line in {hashicorp_list}: {repo_line}")
        else:
            ensure_line_in_file(hashicorp_list, repo_line)
            log("HashiCorp Repository - ensured")

    if not dpkg_installed("terraform") or FORCE_INSTALL:
        apt_update_once()
        step("Install Terraform (apt)", "DEBIAN_FRONTEND=noninteractive apt install -y terraform", allow_fail=False)

    if cmd_exists("terraform"):
        status_ok("Terraform", get_version("terraform version"))
    else:
        status_missing("Terraform")

    # -------------------------
    # DevSecOps / Security Tools
    # -------------------------
    install_cmd_tool(
        "tflint",
        "tflint",
        "tflint --version",
        "curl -fsSL https://raw.githubusercontent.com/terraform-linters/tflint/master/install_linux.sh | bash",
        allow_fail=True
    )

    install_cmd_tool(
        "tfsec",
        "tfsec",
        "tfsec --version",
        "curl -fsSL https://raw.githubusercontent.com/aquasecurity/tfsec/master/scripts/install_linux.sh | bash",
        allow_fail=True
    )

    install_pipx_app("Checkov", "checkov", "checkov", "checkov --version")
    install_pipx_app("Prowler", "prowler", "prowler", "prowler --version")
    install_pipx_app("Semgrep", "semgrep", "semgrep", "semgrep --version")
    install_pipx_app("OpenStack CLI", "openstack", "python-openstackclient", "openstack --version")

    # Trivy (prefer apt attempt; then deb fallback)
    if cmd_exists("trivy") and not FORCE_INSTALL:
        status_ok("Trivy", get_version("trivy --version"))
    else:
        step("Install Trivy (apt attempt)", "DEBIAN_FRONTEND=noninteractive apt install -y trivy || true", allow_fail=True)
        step(
            "Install Trivy (deb fallback if missing)",
            "command -v trivy >/dev/null 2>&1 || ("
            "wget -q https://github.com/aquasecurity/trivy/releases/latest/download/trivy_0.50.1_Linux-64bit.deb -O /tmp/trivy.deb && "
            "dpkg -i /tmp/trivy.deb || apt -f install -y && "
            "rm -f /tmp/trivy.deb )",
            allow_fail=True
        )
        if cmd_exists("trivy"):
            status_ok("Trivy", get_version("trivy --version"))
        else:
            status_missing("Trivy")

    install_cmd_tool(
        "Gitleaks",
        "gitleaks",
        "gitleaks version",
        "curl -fsSL https://raw.githubusercontent.com/gitleaks/gitleaks/master/scripts/install.sh | bash",
        allow_fail=True
    )

    install_cmd_tool(
        "Infracost",
        "infracost",
        "infracost --version",
        "curl -fsSL https://raw.githubusercontent.com/infracost/infracost/master/scripts/install.sh | sh",
        allow_fail=True
    )

    install_apt_packages("Lynis", ["lynis"], allow_fail=True)
    if cmd_exists("lynis"):
        status_ok("Lynis", get_version("lynis --version"))
    else:
        status_missing("Lynis")

    # -------------------------
    # Cloud CLIs
    # -------------------------
    install_cmd_tool(
        "Azure CLI",
        "az",
        "az version",
        "curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash",
        allow_fail=True
    )

    install_cmd_tool(
        "AWS CLI v2",
        "aws",
        "aws --version",
        "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip && "
        "unzip -q /tmp/awscliv2.zip -d /tmp && "
        "/tmp/aws/install --update && "
        "rm -rf /tmp/aws /tmp/awscliv2.zip",
        allow_fail=True
    )

    # Google Cloud CLI (gcloud)
    gcloud_key = "/etc/apt/keyrings/cloud.google.gpg"
    gcloud_list = "/etc/apt/sources.list.d/google-cloud-sdk.list"

    if cmd_exists("gcloud") and not FORCE_INSTALL:
        status_ok("Google Cloud CLI", get_version("gcloud --version"))
    else:
        step(
            "Google Cloud CLI Repo Key",
            "install -m 0755 -d /etc/apt/keyrings && "
            "curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | "
            "gpg --dearmor --yes -o /etc/apt/keyrings/cloud.google.gpg",
            allow_fail=True
        )
        repo_line = "deb [signed-by=/etc/apt/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main"
        if CHECK_ONLY:
            log(f"Google Cloud Repository - SKIPPED (check-only) would ensure line in {gcloud_list}: {repo_line}")
        else:
            ensure_line_in_file(gcloud_list, repo_line)

        apt_update_once()
        step("Install Google Cloud CLI (apt)", "DEBIAN_FRONTEND=noninteractive apt install -y google-cloud-cli", allow_fail=True)

        if cmd_exists("gcloud"):
            status_ok("Google Cloud CLI", get_version("gcloud --version"))
        else:
            status_missing("Google Cloud CLI")

    # -------------------------
    # Languages / Runtimes
    # -------------------------
    install_apt_packages("Java 17 + Maven + Gradle", ["openjdk-17-jdk", "maven", "gradle"], allow_fail=True)

    # Node.js (LTS)
    install_cmd_tool(
        "Node.js (LTS)",
        "node",
        "node --version",
        "curl -fsSL https://deb.nodesource.com/setup_lts.x | bash && DEBIAN_FRONTEND=noninteractive apt install -y nodejs",
        allow_fail=True
    )

    # .NET 8
    if cmd_exists("dotnet") and not FORCE_INSTALL:
        status_ok(".NET SDK", get_version("dotnet --version"))
    else:
        step(
            "Install Microsoft Repo Package",
            f"wget -q https://packages.microsoft.com/config/ubuntu/{version}/packages-microsoft-prod.deb "
            f"-O /tmp/packages-microsoft-prod.deb && "
            "dpkg -i /tmp/packages-microsoft-prod.deb && "
            "rm -f /tmp/packages-microsoft-prod.deb",
            allow_fail=True
        )
        apt_update_once()
        step(
            "Install .NET 8 SDK + Runtime",
            "DEBIAN_FRONTEND=noninteractive apt install -y dotnet-sdk-8.0 dotnet-runtime-8.0 aspnetcore-runtime-8.0",
            allow_fail=True
        )
        if cmd_exists("dotnet"):
            status_ok(".NET SDK", get_version("dotnet --version"))
        else:
            status_missing(".NET SDK")

    # dotnet tools for real sudo user
    if sudo_user:
        bashrc = f"/home/{sudo_user}/.bashrc"
        if CHECK_ONLY:
            log(f"dotnet tools PATH - SKIPPED (check-only) would ensure PATH line in {bashrc}")
        else:
            # Ensure PATH line exists (simple idempotent append)
            path_line = 'export PATH="$PATH:$HOME/.dotnet/tools"'
            try:
                p = Path(bashrc)
                if p.exists():
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                    if path_line not in txt:
                        p.write_text(txt.rstrip() + "\n" + path_line + "\n", encoding="utf-8")
                else:
                    p.write_text(path_line + "\n", encoding="utf-8")
            except Exception:
                pass

        # Install dotnet global tools (best effort)
        step("Install dotnet-ef (user)", f"sudo -u {sudo_user} bash -lc 'dotnet tool install --global dotnet-ef || true'", allow_fail=True)
        step("Install dotnet-format (user)", f"sudo -u {sudo_user} bash -lc 'dotnet tool install --global dotnet-format || true'", allow_fail=True)

    # -------------------------
    # Summary / Next steps
    # -------------------------
    log("\n" + "=" * 70)
    log("DONE")
    log(f"Logs stored at: {LOG_FILE}")
    if not CHECK_ONLY:
        log("Next steps:")
        log("1) Logout/login (or reboot) for Docker access without sudo (docker group).")
        log("2) Re-run anytime; it will show STATUS and skip installed tools.")
    log("=" * 70)

if __name__ == "__main__":
    main()