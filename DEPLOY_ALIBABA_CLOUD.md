# 阿里云 Alibaba Cloud Linux 3 部署说明

## 1. 上传文件

将 `rct-linux-deploy.zip` 上传到服务器后执行：

```bash
sudo dnf install -y unzip
mkdir -p ~/rct-app
unzip -o rct-linux-deploy.zip -d ~/rct-app
cd ~/rct-app
```

## 2. 安装

```bash
sudo bash deploy/install-alibaba-linux.sh
sudo vi /etc/rct/rct.env
```

首次启动前必须在 `/etc/rct/rct.env` 中设置强管理员密码（至少16位，仅由管理员保存）。

## 3. 启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rct nginx
curl http://127.0.0.1:8000/health
```

返回 `{"status":"ok"}` 表示程序正常。

中心账号可导出本中心CSV，管理员可导出全部中心CSV。

误录患者可由本中心账号或管理员删除。系统实际执行“作废”并保留操作者、时间和原因，已显示的随机号不会重新使用。

## 更新已有部署

重新上传并解压新版文件后，在解压目录执行：

```bash
sudo bash deploy/install-alibaba-linux.sh
sudo systemctl daemon-reload
sudo systemctl restart rct nginx
```

安装脚本不会删除 `/var/lib/rct/rct_clean.db`，但更新前仍建议先执行一次备份。

## 4. 阿里云安全组

测试阶段只开放 TCP 80；SSH 22只允许你的固定管理IP访问。不要开放8000端口。部署后测试地址为 `http://<你的服务器IP>`。

绑定域名并配置HTTPS后，开放443并将 `/etc/rct/rct.env` 中的 `RCT_COOKIE_SECURE` 改为 `1`，随后执行：

```bash
sudo systemctl restart rct
```

## 5. 备份

手动备份：

```bash
sudo /usr/local/sbin/backup-rct
```

备份位于 `/var/backups/rct`，默认脚本清理30天以前的备份。正式使用还应将备份复制到另一台设备或对象存储。

## 重要说明

通过公网IP的HTTP地址仅用于虚拟数据测试。录入真实研究数据前必须配置域名、HTTPS、机构审批、访问控制及异地备份。
