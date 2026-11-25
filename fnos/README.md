# SweetReader · 飞牛 fnOS 官方应用中心上架包

飞牛官方应用中心走**邀请制人工审核**（不是 GitHub PR，也不开放自助直接提交）。
本目录收录已备好的 fpk 包与提交材料，剩余步骤需开发者本人在开发者平台完成。

## 当前状态

| 项 | 状态 |
|---|---|
| fpk 包（x86） | ✅ 已用官方 fnpack 1.3.3 构建，`sweetreader-1.0.0-x86.fpk` |
| manifest | ✅ 三段式版本、platform=x86、source=thirdparty、desc 单行无注释符 |
| Docker 镜像 | ✅ `docker.io/514475844/sweetreader:latest`（多架构，Release 自动构建） |
| 图标 | ✅ 64×64 / 256×256 方形图标 |
| 数据持久化 | ✅ 落飞牛托管共享目录 `/var/apps/sweetreader/shares/...` |
| 端口 | ✅ 容器绑 `127.0.0.1:5000`（不监听公网，符合官方规则 2） |
| 包签名 | ⚠️ 未签名（本地调试包）。正式提交需导入开发者证书后重打 |
| x86 真机验证 | ✅ 已在 x86 飞牛上跑通（换机前的旧设备，已下线迁至 156）；现役 156 / 旧 198 OEC 均为 aarch64 |
| 开发者账号 | ⚠️ 需本人在 developer.fnnas.com 注册并申请加入开发者先锋交流群 |

## 剩余步骤（需真人操作）

1. **注册开发者**：`developer.fnnas.com` → 注册开发者账号。
2. **进交流群**：飞牛官网右上「关注飞牛」→「微信群」→ 扫码进粉丝群 → 联系社区主理人，申请加入「应用中心开发者先锋交流群」。
3. **导入签名证书**：按开发者后台指引生成 key/crt，`fnpack build --key <key> --cert <cert>` 重打签名包（替换当前未签名调试包）。
4. **x86 真机复验**（可选）：换机前的 x86 飞牛已跑通过；若审核要求最新证据，可在 x86 设备上 `appcenter-cli install-fpk sweetreader-1.0.0-x86.fpk` 复验。
5. **提交表单**：在开发者后台「我的应用」填写上架申请，上传签名 fpk、应用截图、填写联系方式，等待审核（首版按 1–2 周预期）。

## 重新构建 fpk

```bash
# 下载官方 fnpack：https://developer.fnnas.com/docs/cli/fnpack/
cd src                       # 本目录的 sweetreader/ 项目副本
fnpack build --nosign        # 本地调试
# 正式提交：
fnpack build --key developer.key --cert developer.crt
```

## 本地测试（在飞牛设备上）

```bash
appcenter-cli install-fpk sweetreader-1.0.0-x86.fpk
appcenter-cli list            # 确认状态
appcenter-cli stop sweetreader / start sweetreader
```
