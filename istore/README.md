# SweetReader · iStoreOS 应用商店上架包

把 SweetReader 打包成 iStoreOS（OpenWrt 软件中心）的一键安装应用，供路由器 / NAS 用户安装。

## 目录结构

```
istore/luci-app-sweetreader/
├── Makefile                      # OpenWrt 包：安装 LuCI 入口 + 默认配置 + 元数据
├── appstore.json                 # iStoreOS 应用元数据（商店展示 + 部署依据）
├── docker-compose.yml            # 服务定义（镜像 / 端口 / 持久化卷）
├── luasrc/                       # LuCI 控制器与页面
│   ├── controller/sweetreader.lua
│   └── view/sweetreader/main.htm
├── po/zh-cn/sweetreader.po       # 中文本地化
├── root/etc/config/sweetreader   # UCI 默认配置
└── root/usr/share/sweetreader/   # 安装到设备后的元数据副本（icon.png + appstore.json + docker-compose.yml）
```

## 上架前置条件（均已就绪）

1. **公开仓库**：✅ `github.com/514475844/SweetReader` 已公开。整个 `istore/luci-app-sweetreader/`
   目录已随源码仓库发布。
2. **Docker 镜像**：✅ `docker.io/514475844/sweetreader:latest` 已推送到 Docker Hub（多架构 amd64/arm64，
   由 Release 标签自动构建）。`docker-compose.yml` 已引用该真实镜像。
3. **图标与截图**：✅ `icon.png` 复用项目 logo；`appstore.json` 的 3 张 screenshot 均为仓库内真实截图
   （reader / dark / toc），URL 可直接访问。
4. **开源协议**：✅ GPL-3.0（仓库根 `LICENSE` 已就位，利于 iStoreOS 收录审核）。

## 提交步骤

iStoreOS 的应用元数据来自 `linkease/openwrt-app-actions` 的 `applications/` 目录（或对应的 meta 仓库）。
两种收录方式：

**方式 A：向官方 meta 仓库提 PR（推荐）**
1. Fork `https://github.com/linkease/openwrt-app-actions`。
2. 把 `luci-app-sweetreader/` 整个目录放到 `applications/luci-app-sweetreader/`。
3. 确保 `applications/luci-app-sweetreader/appstore.json` 与 `docker-compose.yml` 就位。
4. 提 PR，等待审核合入；合入后商店会自动出现「SweetReader 个人书库」。

**方式 B：自有公开仓库 + 社区提交**
1. 建公开仓库，把 `istore/luci-app-sweetreader/` 内容放根目录（保证 `appstore.json` 在仓库根或 `applications/<name>/` 下）。
2. 在 iStoreOS 社区 / 酷友社论坛发帖，附仓库地址，按版主要求补充信息。

## 用户侧安装后

- 在 iStore 搜「SweetReader」→ 安装（自动拉起 Docker 容器，映射 5000 端口）。
- 浏览器打开 `http://<路由器IP>:5000/`。
- 首次启动初始管理员密码打印在 `docker logs sweetreader`，登录后请尽快改密。
- 书库存放目录默认 `/mnt/sweetreader`，可在 `docker-compose.yml` 用 `BOOK_DIR` 环境变量覆盖。

## 已支持格式

EPUB、PDF、DOC / DOCX、TXT（含分类、搜索、阅读进度、双页、排版设置、TXT 段落重排与自动目录）。
