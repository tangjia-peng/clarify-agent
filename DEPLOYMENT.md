# Mellow：部署成别人可以使用的网址

这个版本将网页、Python Agent 和两个 MCP 工具放在同一个 Docker 服务中。
推荐先用 Render 的 Free Web Service 验证，平台会生成 HTTPS 地址，不必先购买域名。
目前仓库提供部署文件，尚未创建 Render 账号、上传代码或生成公网网址。

## 已准备的能力

- 多轮对话、流式回答、执行记录、中途询问与取消继续使用原来的 Agent。
- 公网入口要求访问口令，登录有效期默认 12 小时；cookie 不包含口令。
- 每次登录创建独立的文件目录，不再把项目代码目录开放给网页用户。
- 登录后右上角的“我的文件”可下载本次登录中保存的文件。
- 公网模式禁用 fetch 的内网地址访问；部署时预装工具，无需首问再下载 MCP 包。
- 默认最多两个网页连接同时使用；每个标签页占一个连接，关闭标签页释放。

这是邀请制的小规模演示版本，采用共享访问口令，不包含账号注册、数据库或长期聊天记录。
文件位于服务器，不会直接读写访客自己电脑上的磁盘。

## 首次在 Render 上部署

1. 注册自己的 [GitHub](https://github.com/signup) 和 [Render](https://dashboard.render.com/register) 账号。
2. 在 GitHub 新建一个私有仓库，将本项目代码上传到仓库根目录。也可以解压 `dist/mellow-deploy.zip` 后上传其内容；该包不包含本地 `.env`、虚拟环境、缓存和生成的用户文件。
3. 在 Render 选择 **New → Blueprint**，连接这个仓库，读取根目录的 `render.yaml`。
4. 确认服务类型为 Web Service、runtime 为 Docker、套餐为 **Free**。不要选择付费套餐或添加付费磁盘。
5. 按提示在 Render 页面填写环境变量。密钥只填在平台的 Environment/Secret 设置里，不提交到 GitHub：

   - `OPENAI_API_KEY`、`OPENAI_BASE_URL`：沿用原项目的模型接口配置。
   - `EMBEDDING_KEY`、`EMBEDDING_BASE_URL`：沿用原项目的向量接口配置。
   - `OPENAI_MODEL`、`EMBEDDING_MODEL`：模板使用现有默认值，也可改为你的接口实际支持的模型。
   - `WEB_ACCESS_PASSWORD`：为访客设置一个 **12～256 字符**的访问口令，勿与模型密钥或账号密码复用。

6. 创建/部署服务，等待构建日志完成。若没有开始，使用 **Manual Deploy → Deploy latest commit**。
7. 打开 Render 显示的 `https://……onrender.com`，输入访问口令，发送一条问题。确认正常后，再把网址和访问口令发给需要试用的人。

Render 自动提供 `RENDER_EXTERNAL_URL`，程序会用它核对网页与 WebSocket 的来源。
如果以后绑定自有域名，在环境变量里把 `WEB_PUBLIC_URL` 设置为那个完整的 HTTPS 网址。
网址只填写域名及可选端口，不带子路径。API 基础地址保持原格式，向量基础地址不加末尾 `/embeddings`。

`render.yaml` 关闭了自动部署；以后更新 GitHub 代码后，手动触发部署即可。
本版本的登录状态、连接限制和对话都在单个进程的内存里，应保持 **一个服务实例、一个 Uvicorn worker**。

## 免费实例的实际限制

Render Free 连续 15 分钟没有 HTTP 请求或 WebSocket 消息会休眠，下次打开通常需要约一分钟唤醒。
休眠、重新部署、进程重启或浏览器刷新会结束原对话；服务重启也会使登录失效。
免费服务的文件系统不持久化，生成文件请及时下载。即使使用本地 Docker 卷，重新登录也不会自动找回旧登录的目录。
对话量较大时，模型与向量接口仍按你的服务商规则计费；Render 免费套餐并不会免除这些接口费用。
先用少量朋友验证；资源不足可减少 `WEB_MAX_CONNECTIONS`，需要稳定在线或保存文件时再选付费实例与持久化方案。

官方依据：[Docker 部署](https://render.com/docs/docker)、[WebSocket](https://render.com/docs/websocket)、[免费实例限制](https://render.com/docs/free)、[默认环境变量](https://render.com/docs/environment-variables)。

## 本机可选 Docker 验证

这台电脑尚未安装 Docker，因此本次没有执行镜像构建。可直接交给 Render 构建，或在安装 Docker Desktop 后本机验证：

1. 在现有 `.env` 增加 `WEB_ACCESS_PASSWORD=你自己设置的长口令`。本机 Docker 测试时保持 `WEB_PUBLIC_URL` 为空，或设为 `http://localhost:8765`。
2. 停止占用 8765 端口的旧服务，然后在项目目录执行：

```powershell
docker compose up --build -d
docker compose logs -f
```

3. 打开 **http://localhost:8765**（与配置域名保持一致），输入访问口令。
4. 测试完成后执行 `docker compose down`，默认保留 Docker 数据卷。

原有本地入口不需要以上部署变量，仍可执行 `uv run --cache-dir .uv-cache web_main.py`；终端对话仍执行 `uv run main.py`。

## 已验证与待验证

- 自动测试覆盖登录、来源校验、口令尝试限制、连接上限、过期/退出、下载路径与不同登录间的文件隔离，不调用外部模型或向量接口。
- 固定版本的 fetch 与 filesystem MCP 已在本机完成真实启动、协议初始化、文件读写和越界访问拒绝测试。
- 浏览器实测通过登录、提问→中途答复→生成文件→下载→退出；第二次登录无法下载第一次登录的文件，手机端登录后新增按钮也在视口内。生成模型响应在这项检查中使用模拟实现。
- Dockerfile 使用 Python 3.12、Node.js 24，固定 fetch 为 `2026.11.11`、filesystem 为 `2.3.0`，应用依赖使用 `uv.lock`。
- Docker 镜像的实际 Linux 构建，以及 Render 上的 HTTPS、模型接口连通性，需要首次部署后确认；当前没有把本地验证当作公网部署成功。

如需修改并发上限，设置 `WEB_MAX_CONNECTIONS`（1～100，免费模板默认 2）。
登录有效期通过 `WEB_SESSION_SECONDS` 设置（60～604800 秒）。
需要撤销所有登录时，在 Render 修改访问口令并重新部署，旧进程中的登录随重启失效。
