# RHEL 9.6 is within CRC’s supported Linux host range, because CRC supports the latest two RHEL minor releases.
# You have about 30.6 GiB available RAM, which is well above CRC’s minimum 10.5 GB free memory for the OpenShift preset.
# You have 66 GB free on / and 212 GB free on /home, which is above CRC’s minimum 35 GB storage requirement.

clean and refresh dnf:
sudo dnf clean all
sudo dnf makecache

# RHEL
sudo subscription-manager register
sudo subscription-manager attach --auto
sudo subscription-manager refresh
sudo subscription-manager status

# enable the required RHEL 9 repos manually:
sudo subscription-manager repos --enable=rhel-9-for-x86_64-baseos-rpms
sudo subscription-manager repos --enable=rhel-9-for-x86_64-appstream-rpms


# Run these on the RHEL VM: 
nproc
lscpu | egrep 'CPU\(s\)|Model name|Virtualization|Hypervisor'
sudo virt-host-validate
lsmod | grep kvm
cat /sys/module/kvm_intel/parameters/nested 2>/dev/null || cat /sys/module/kvm_amd/parameters/nested 2>/dev/null

sudo dnf install -y libvirt NetworkManager
sudo systemctl enable --now libvirtd
sudo systemctl enable --now NetworkManager

# Then start the RHEL 9 virtualization services. Red Hat’s current RHEL 9 docs use the socket-based virt* daemons rather than relying only on libvirtd.service.

sudo systemctl enable --now NetworkManager
for drv in qemu network nodedev nwfilter secret storage interface; do
  sudo systemctl enable --now virt${drv}d.socket virt${drv}d-ro.socket virt${drv}d-admin.socket
done

# Now verify virtualization:

sudo virt-host-validate
ls -l /dev/kvm

# add your user to the libvirt group and re-login:

sudo usermod -aG libvirt $USER
newgrp libvirt

# official Red Hat OpenShift Local page here and select Linux after signing in and get your pull secret from the official OpenShift download page here:
https://developers.redhat.com/products/openshift-local
https://developers.redhat.com/products/openshift/download

tar -xvf crc-linux-*.tar.xz
sudo cp crc-linux-*/crc /usr/local/bin/
crc version

crc setup
crc start -p ~/.crc/pull-secret

crc status
oc whoami
oc get nodes
oc get pods -A

crc config set cpus 8
crc config set memory 24576
crc config set disk-size 120

crc console
crc console --credentials

# If it says Running, then CRC is up and you can continue. CRC documents crc console --credentials for retrieving the developer and kubeadmin passwords after startup.

Then check the cluster:

crc oc-env
eval "$(crc oc-env)"
oc login -u kubeadmin -p '<password-from-credentials>' https://api.crc.testing:6443
oc get nodes
oc get co

# To stop and start later:

crc stop
crc start

# If crc status does not show Running, inspect the log:

tail -n 100 ~/.crc/crc.log

# NOTE: 
let another machine on your LAN open the CRC web console and API running on the RHEL VM. CRC’s docs call this “Setting up CRC on a remote server”, and they explicitly say it works only with system-mode networking.

Why crc start comes first

Because those HAProxy rules need a real CRC instance already running.
export CRC_IP=$(crc ip)

This line:
takes the IP address of the running CRC instance and uses it as the backend target for HAProxy. CRC’s networking docs show that crc ip points to the CRC instance, and in Linux system mode CRC uses an internal IP in the 192.168.130.0/24 range; in your case it returned 127.0.0.1, which indicates user-mode networking instead.
So the intended flow was:

crc start

crc ip gets the CRC instance IP

HAProxy forwards LAN traffic to that CRC instance

your Mac opens the console/API through the RHEL VM

What each part was doing

sudo dnf install -y haproxy firewalld
Installs the reverse proxy and firewall tools needed for remote access on the RHEL VM. CRC’s remote-server procedure specifically uses HAProxy for this setup.

sudo systemctl enable --now firewalld
Turns on the firewall service so you can open only the ports CRC needs remotely.

sudo firewall-cmd --add-service=http --permanent
sudo firewall-cmd --add-service=https --permanent
sudo firewall-cmd --add-service=kube-apiserver --permanent
sudo firewall-cmd --reload

Opens:

80 for app HTTP routes

443 for app HTTPS routes and console

6443 for the OpenShift API server

CRC’s docs use those exact firewall services for remote access.

sudo semanage port -a -t http_port_t -p tcp 6443
SELinux normally does not let HAProxy bind arbitrary ports. This command allows HAProxy to listen on 6443, which is the Kubernetes/OpenShift API port. CRC’s docs explicitly include this SELinux step.

sudo cp /etc/haproxy/haproxy.cfg{,.bak}
Just makes a backup before replacing the config.

Then this config:

listen apps
    bind 0.0.0.0:80
    server crcvm $CRC_IP:80 check

listen apps_ssl
    bind 0.0.0.0:443
    server crcvm $CRC_IP:443 check

listen api
    bind 0.0.0.0:6443
    server crcvm $CRC_IP:6443 check

means:

HAProxy listens on the RHEL VM’s LAN IP

and forwards:

port 80 to the CRC instance port 80

port 443 to the CRC instance port 443

port 6443 to the CRC instance port 6443

That is how a second machine, like your Mac, can reach:

https://console-openshift-console.apps-crc.testing

https://api.crc.testing:6443

sudo systemctl enable --now haproxy
Starts the proxy so the forwarding actually works.

Why it failed in your case

Because your CRC instance was in user-mode networking, and crc ip returned:

127.0.0.1

CRC’s docs indicate user mode behaves differently, and the remote-server procedure works only in system mode. With 127.0.0.1, CRC is effectively bound to the local host, so your Mac cannot reach it over the network.

You also discovered another issue: CRC itself was already listening on 80, 443, and 6443, so HAProxy had no free ports to bind. That is why the service would not start.

So the simple explanation is

That whole command block meant:

“Take a CRC cluster already running on the RHEL VM and publish it safely to your local network so your Mac can open the console and API.”

But for that to work, CRC must be:

already started

using system-mode networking

reachable via a non-loopback CRC IP

Right now, because crc ip is 127.0.0.1, the correct next step is not HAProxy.
It is switching CRC to system mode and recreating the instance.

If you want, I’ll give you a clean “why user mode vs system mode” explanation next, in simple lab terms.

@@@@###################################
export CRC_IP=$(crc ip)

sudo dnf install -y haproxy firewalld
sudo systemctl enable --now firewalld

sudo firewall-cmd --add-service=http --permanent
sudo firewall-cmd --add-service=https --permanent
sudo firewall-cmd --add-service=kube-apiserver --permanent
sudo firewall-cmd --reload

sudo semanage port -a -t http_port_t -p tcp 6443 2>/dev/null || true

sudo cp /etc/haproxy/haproxy.cfg{,.bak}

sudo tee /etc/haproxy/haproxy.cfg >/dev/null <<EOF
global
    log /dev/log local0

defaults
    balance roundrobin
    log global
    maxconn 100
    mode tcp
    timeout connect 5s
    timeout client 500s
    timeout server 500s

listen apps
    bind 0.0.0.0:80
    server crcvm $CRC_IP:80 check

listen apps_ssl
    bind 0.0.0.0:443
    server crcvm $CRC_IP:443 check

listen api
    bind 0.0.0.0:6443
    server crcvm $CRC_IP:6443 check
EOF

sudo systemctl enable --now haproxy

Config is fine. When haproxy -c says Configuration file is valid but the service still will not start, the two most likely causes are:

SELinux blocking HAProxy on port 6443

another service already using 80, 443, or 6443

CRC’s remote-server guide specifically requires allowing HAProxy to listen on TCP 6443 with semanage, and using listeners on 80, 443, and 6443.

Run these on the RHEL VM:

sudo dnf install -y policycoreutils-python-utils

sudo semanage port -l | grep 6443
sudo semanage port -a -t http_port_t -p tcp 6443 || \
sudo semanage port -m -t http_port_t -p tcp 6443

sudo ss -ltnp | egrep ':80|:443|:6443'
sudo systemctl status haproxy --no-pager -l
sudo journalctl -xeu haproxy.service --no-pager | tail -50

sudo firewall-cmd --add-service=http --permanent
sudo firewall-cmd --add-service=https --permanent
sudo firewall-cmd --add-service=kube-apiserver --permanent
sudo firewall-cmd --reload