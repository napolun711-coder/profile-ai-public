# 本地知识库助手

一个面向个人与小型团队的本地资料知识库应用：导入文档、经过解析与质量门、确认后构建检索索引，并通过聊天获得带来源的回答。

项目使用 **FastAPI + LightRAG + SQLite**。运行数据与配置默认存放于用户目录，不写入源码仓库。

## 一键安装

Windows 10/11：

1. 双击 `setup.cmd`
2. 等待依赖、LightRAG 和嵌入模型安装完成
3. 首次使用打开设置页，填写生成模型 API Key

安装器会：

- 安装或检查 uv；即使 winget 安装后 PATH 未刷新，也会自动使用绝对路径或官方安装包
- 安装本应用依赖
- 在 `%LOCALAPPDATA%\ProfileAI\backend\lightrag` 安装固定版本 LightRAG `1.5.6`
- 下载 LightRAG 官方 `env.example` 作为默认 `.env`
- 安装/启动 Ollama 并拉取 `bge-m3`
- 创建桌面与开始菜单快捷方式
- 启动应用并打开浏览器

安装可重复运行，具有幂等修复作用。

## 数据位置

默认根目录：

```text
%LOCALAPPDATA%\ProfileAI
  config\
  data\
  knowledge\
  rag_storage\
  backend\lightrag\
  logs\
  output\
  backups\
```

可用环境变量 `PROFILE_AI_HOME` 覆盖整个数据根目录。

源码目录只保留程序、模板和测试。

## 配置

用户配置统一保存在：

```text
%LOCALAPPDATA%\ProfileAI\config\user-settings.json
```

设置页可修改：

- LLM 提供方、模型、接口地址和 API Key
- Embedding 提供方、模型、接口地址和 API Key
- Rerank
- 向量存储
- 分块大小、分块重叠、实体/关系召回数、文本片段召回数
- 质量门、入库确认、OCR、记忆状态

LightRAG 使用官方默认 `.env`，模型与敏感配置由启动器从用户设置注入，不需要手工编辑 LightRAG `.env`。

## 启动与停止

- 启动：桌面快捷方式、`appctl.vbs` 或 `start.cmd`
- 停止：桌面“停止”快捷方式、`appctl.bat stop` 或 `stop.cmd`
- 状态：`appctl.bat status`

启动器会检测 Windows 保留端口。若默认端口不可用，会自动选择可用端口并写入：

```text
%LOCALAPPDATA%\ProfileAI\config\runtime.json
```

无需为端口冲突重启电脑。

## 推荐浏览器与朗读

推荐 **Microsoft Edge**。朗读使用浏览器自带的 `speechSynthesis`，本应用不下载也不运行语音模型。

Edge 桌面端“应用 → 将此站点安装为应用”，移动端“添加到主屏幕”，可以像普通应用一样打开。

中文语音输入推荐豆包输入法（若设备支持）：长按空格输入，完成后直接点击输入法旁的发送按钮。

## 手机 / 平板访问

1. 电脑与移动设备连接同一局域网
2. 运行一次 `allow_lan.bat`
3. 在 `.env` 中临时设置 `APP_HOST=0.0.0.0`，或通过部署环境设置
4. 重启应用，并在设置页查看局域网地址
5. 手机打开 `http://电脑IP:8100/web`
6. 首次访问在电脑设置页确认设备

不要在路由器上做公网端口映射。

## 开发

```powershell
uv sync
.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
.venv\Scripts\python.exe scripts\start.py
```

## 文档

- [使用说明](docs/使用说明.md)
- [隐私与数据](docs/隐私与数据.md)
- [迁移报告](MIGRATION_REPORT.md)

## 许可证

[MIT](LICENSE)。第三方组件与模型遵循各自许可。
