# 📚 SweetReader · 私人电子书库

**SweetReader** 是一个轻量级的自托管电子书阅读器，支持 EPUB、PDF、TXT、DOCX、MOBI 等
多种格式。你可以把它部署在 NAS、软路由或任何 Linux 设备上，随时随地阅读自己的书库。

当前版本：**2.4**（[更新日志](CHANGELOG.md)）

---

## ✨ 功能特性

**阅读**

- 沉浸式阅读：禅模式、全屏、字号 / 行距 / 亮度 / 页面颜色可调，设置按账号持久化
- 背景纹理：格子 / 点阵 / 横线 / 羊皮纸，纯样式实现，不加载外部图片
- 章节目录：自动识别常见章节格式，支持目录跳转、章节下拉快捷跳转、上 / 下一章
- 触摸手势：左右滑动切换章节、双指捏合调节字号，适配横屏
- 点触翻页：左侧上翻、右侧下翻、中间区域切换工具栏
- 页内搜索：在当前书籍正文中搜索并高亮
- 阅读进度：百分比显示、进度条拖拽跳转、继续阅读入口
- 快捷键：`Z` 禅模式、`D` 切换主题、`Ctrl` + 滚轮调字号与行距
- 主题：默认 / 暗色 / 护眼，可跟随系统，按账号保存

**书库**

- 分类导航：扁平列表 + 逐级下钻，目录树按需懒加载，展开状态本地记忆
- 检索：书名 / 作者 / 文件名 / 标签搜索，带防抖与格式、分类分布统计
- 排序与筛选：按书名 / 作者 / 时间 / 阅读进度排序，按字母与格式筛选
- 阅读状态：未读 / 在读 / 已读
- 标签：自由打标签，标签参与搜索
- 书籍详情页：元数据、封面、分类面包屑与相关推荐
- 中文拼音归类：中文书名按拼音首字母分组，自动跳过 `《` `【` `[` 等前缀符号
- 书签：随时添加，自动摘录当前位置上下文便于回看

**后台**

- 书籍管理：导入（含 ZIP 批量）、封面自定义、信息编辑、合并、批量操作、元数据导出 CSV
- 分类管理：新建 / 重命名 / 合并，删除带安全熔断
- 去重与清理：重复书籍查重去重、失效记录清理
- 用户体系：注册 / 登录、邀请码、用户管理与在线状态、操作日志
- 权限：最高管理员可授权**最多 3 名**子管理员，按「导入 / 导出 / 书籍管理」分别授权

**插件**

- 插件化扩展：插件可带来独立页面、首页入口、管理侧栏链接与阅读页工具栏按钮
- 图形化设置：插件声明字段后由「插件管理」页自动生成表单，插件作者无需写前端
- 支持放入自己的插件文件，管理页可随时重新扫描注册

**其它**

- 智能编码：自动检测 GBK / GB18030 / BIG5 / UTF-8
- 异步扫描：后台线程扫描 + 实时进度，扫描期间站点照常可用
- 容器健康检查，异常自动重启

---

## 🚀 快速部署

### Docker 一键运行

```bash
docker run -d \
  --name sweetreader \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /path/to/your/books:/app/books:ro \
  -v /path/to/data:/app/instance \
  514475844/sweetreader:latest
```

也可以在 `docker-compose.yml` 中自行组织同样的三个挂载与端口映射。

### 获取初始管理员密码

**本项目不提供任何默认口令。** 首次启动时会自动创建管理员账号，并把凭据**打印到容器日志**：

```bash
docker logs sweetreader
```

输出形如：

```
[security] ========================================================
[security] 已随机生成管理员账号，请立即保存并登录后修改：
[security]   用户名: admin_root
[security]   密  码: xxxxxxxxxxxxxxxx
[security] 该口令只会显示这一次，之后无法找回。
[security] ========================================================
```

口令遗失时，可通过环境变量重置后重启容器：

```bash
docker run -d -e SR_ADMIN_PASSWORD="你的新密码" ...
```

| 环境变量 | 说明 |
|----------|------|
| `SR_ADMIN_USERNAME` | 管理员用户名，默认 `admin_root` |
| `SR_ADMIN_PASSWORD` | 管理员口令。设置后会覆盖已有口令，用于重置 |

### 安全提示

- 该管理员账号不会出现在后台用户列表中，但功能等同于管理员
- 启动时会扫描常见弱口令，命中即自动停用该账号
- 建议部署在内网或通过反向代理加 TLS，不要直接暴露公网

### 参数说明

| 参数 | 说明 |
|------|------|
| `-p 5000:5000` | 访问端口，访问地址为 `http://你的IP:5000` |
| `-v /path/to/books:/app/books:ro` | 挂载书籍目录（建议只读） |
| `-v /path/to/data:/app/instance` | 挂载数据目录（数据库、密钥、插件配置等） |
| `--restart unless-stopped` | 容器异常退出后自动重启 |
| `-d` | 后台运行 |

### 健康检查

镜像内置健康检查（60s 间隔、45s 启动宽限、3 次重试），配合 `--restart unless-stopped`
可在服务异常时自动重启：

```bash
docker inspect --format '{{.State.Health.Status}}' sweetreader   # healthy
```

---

## 📁 项目结构

```
SweetReader/
├── app/                     # 核心应用
│   ├── __init__.py          # Flask 应用工厂
│   ├── models.py            # 数据模型（Book / Category / User / Bookmark / ReadingProgress …）
│   ├── routes.py            # 路由与 API
│   ├── utils.py             # 工具函数（含拼音首字母归类）
│   ├── category_scanner.py  # 分类扫描器
│   ├── encoding_utils.py    # 编码检测
│   └── plugins/             # 插件框架与内置 / 示例插件
├── templates/               # HTML 模板
├── static/                  # 静态资源（含 epubjs / pdfjs）
├── docs/                    # 插件规范与开发指南
├── docker-entrypoint.sh     # 启动初始化脚本
├── Dockerfile
├── requirements.txt
├── run.py                   # waitress 生产服务器入口
├── CHANGELOG.md
└── README.md
```

---

## 🔧 从源码构建

```bash
git clone https://github.com/514475844/SweetReader.git
cd SweetReader
docker build -t sweetreader:local .
```

开发模式需要 Python 3.11+：

```bash
pip install -r requirements.txt
export BOOKS_DIR=/path/to/books
python run.py
```

---

## 🧩 插件

插件让功能可以按需增删，而不必改动核心代码。

- **放置位置**：把插件文件放进数据目录下的 `plugins/`（即容器内 `/app/instance/plugins`），
  该目录是持久卷，重新部署不会丢失
- **加载失败不影响运行**：有问题的插件会在「插件管理」页标红并显示错误原因
- **无需重启**：放入新文件后点一下「重新扫描插件目录」即可注册
- **图形化设置**：声明字段后由管理页自动生成表单，保存即时生效
- **开发文档**：详见 [插件规范标准](docs/插件规范标准.md)（权威规格）与
  [插件开发指南](docs/插件开发指南.md)（快速上手）

---

## 📋 待开发功能

- [ ] 翻页动效（仿真翻页 / 平滑翻页）
- [ ] 插件上传入口（直接上传单个文件或压缩包）
- [ ] 全库乱码与损坏文件的检测、修复与清理
- [ ] OPDS 支持
- [ ] 全文搜索（FTS5）

---

## 🙏 致谢

阅读器内核使用 [epub.js](https://github.com/futurepress/epub.js) 与
[PDF.js](https://mozilla.github.io/pdf.js/)。

---

## 📄 许可证

MIT License，详见 [LICENSE](LICENSE)。
