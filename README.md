# 视频重制工坊

这是从原方案仓库整理出的开发起点，保留可运行静态原型和必要产品资料，剔除了飞书导出、验证截图、归档版本、临时文件和文档生成脚本。

## 目录

```text
src/
  index.html
  styles.css
  app.js
docs/
  product-plan-v3.16.md
  technical-terms.md
```

## 本地预览

直接打开：

```text
src/index.html
```

或启动本地静态服务：

```powershell
npm run dev
```

默认地址：

```text
http://localhost:5173
```

## MVP 工程预览

当前仓库已新增本地单机 MVP 工程骨架：

```text
backend/   FastAPI + SQLite + 本地文件存储
frontend/  React + TypeScript + Vite
config/    Agent 节点提示词和模型配置
```

启动后端：

```powershell
npm run backend
```

默认地址：

```text
http://localhost:8003
```

当前Cloudflare免费桥接任务 `VideoRemakeWorkshop-Bridge` 会在当前Windows用户登录时启动。如果后端8003已经健康，任务会直接复用；如果没有进程监听8003，任务会用本项目的Python环境启动同一个FastAPI入口并等待健康检查。若8003已被占用但健康检查失败，任务会保留该进程并记录错误，不会强行结束或再启动一个后端。电脑需保持开机、唤醒并联网；查看桥接与后端日志可打开 `%LOCALAPPDATA%\VideoRemakeWorkshop\logs`。也可手动运行 `npm run backend` 启动后端。

启动前端：

```powershell
npm run frontend
```

默认地址：

```text
http://localhost:5174
```

前端依赖首次安装：

```powershell
npm --prefix frontend install
```

后端基础检查：

```powershell
npm run check:backend
```

前端构建检查：

```powershell
npm --prefix frontend run build
```

### 模型配置

默认使用 `mock` 模式：

```json
{
  "mode": "mock"
}
```

切换真实火山方舟接口时，修改 `config/models.json`：

```json
{
  "mode": "volcengine"
}
```

本地启动后端前设置 API Key。不要把 Key 写入仓库文件。

```powershell
$env:ARK_API_KEY="你的方舟 API Key"
npm run backend
```

方舟 OpenAI 兼容接口默认地址：

```text
https://ark.cn-beijing.volces.com/api/v3
```

还需要在 `config/models.json` 中补齐模型或推理接入点 ID：

```json
{
  "volcengine": {
    "textModel": "doubao-seed-2-1-pro-260628",
    "vlmModel": "doubao-seed-2-1-pro-260628",
    "imageModel": "",
    "videoModel": ""
  }
}
```

当前后端已经接好 Ark OpenAI-compatible `responses` 文本 JSON 通路。视频文件多模态输入、图片生成和视频生成的真实请求格式，需要根据你实际使用的豆包模型或推理接入点继续补齐。

设置 API Key 并切换到 `volcengine` 模式后，可以先测试文本模型连通性：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/api/model-test/chat `
  -ContentType "application/json" `
  -Body '{"prompt":"请回复模型连接正常"}'
```

## 清理口径

已保留：

- 当前静态原型源码。
- 当前 v3.16 产品方案。
- 术语说明。

已剔除：

- 飞书导入、导出和重建脚本。
- 批量截图、线框图、QA 校验图。
- 历史归档 PRD。
- 临时目录、缓存目录和个人工具配置。
