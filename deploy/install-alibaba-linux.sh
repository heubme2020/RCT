#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "请使用 sudo bash install-alibaba-linux.sh 运行"
  exit 1
fi

dnf install -y python38 nginx sqlite
id rct >/dev/null 2>&1 || useradd --system --home /opt/rct --shell /sbin/nologin rct
install -d -o rct -g rct -m 0750 /opt/rct /var/lib/rct
install -d -o root -g rct -m 0750 /etc/rct
install -m 0644 app.py /opt/rct/app.py
rm -rf /opt/rct/static
cp -a static /opt/rct/static
chown -R root:root /opt/rct/static
install -m 0644 deploy/rct.service /etc/systemd/system/rct.service
install -m 0644 deploy/nginx-rct.conf /etc/nginx/conf.d/rct.conf
if [[ ! -f /etc/rct/rct.env ]]; then
  install -m 0640 -o root -g rct deploy/rct.env.example /etc/rct/rct.env
fi
install -m 0750 deploy/backup-rct.sh /usr/local/sbin/backup-rct

echo "请先编辑 /etc/rct/rct.env 设置强管理员密码，然后执行："
echo "systemctl daemon-reload && systemctl enable --now rct nginx"
