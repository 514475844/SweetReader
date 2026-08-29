# 📚 SweetReader - 私人电子书库

**SweetReader** 是一个轻量级的自托管电子书阅读器，支持 EPUB、PDF、TXT、DOCX、MOBI 等多种格式。你可以把它部署在 NAS、软路由或任何 Linux 设备上，随时随地阅读你的私人书库。

---

## ✨ 功能特性

- **沉浸式阅读**：全屏无白框，A+/A- 调节字号、行距、边距
- **全局主题**：默认/暗色/护眼，按账号保存
- **书库管理**：目录树分类、分页加载、多格式筛选
- **智能编码**：自动检测 GBK/GB18030/BIG5/UTF-8
- **移动端适配**：完美适配手机/平板
- **用户系统**：注册/登录、邀请码、管理员后台
- **进度保存**：滚动自动保存，首页继续阅读入口

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
  sweet-reader:latest
```

### 默认账号

| 字段 | 值 |
|------|------|
| 用户名 | admin |
| 密码 | admin123 |

---

## 📁 项目结构

```
SweetReader/
├── app/                 # 核心应用
│   ├── __init__.py      # Flask 应用工厂
│   ├── models.py        # 数据库模型
│   ├── routes.py        # 路由与 API
│   ├── utils.py         # 工具函数
│   ├── category_scanner.py  # 分类扫描器
│   └── encoding_utils.py    # 编码检测
├── templates/           # HTML 模板
├── static/              # 静态资源
├── Dockerfile
├── requirements.txt
├── run.py
└── README.md
```

---

## 📋 待开发功能

- [ ] 增量扫描
- [ ] 全屏阅读模式
- [ ] OPDS 支持
- [ ] 书籍元数据解析
- [ ] 全文搜索
- [ ] 阅读统计
- [ ] 插件系统

---

## 📄 许可证

MIT License

---

**SweetReader - 享受阅读，智慧时光** 📚
