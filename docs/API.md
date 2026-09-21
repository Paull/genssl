# 在线证书管理 API

服务默认监听本机地址（具体端口由启动参数决定），接口以 `/api` 为前缀。所有请求和响应使用 JSON；下载接口返回文件流。v3 面向单机自用场景，支持管理员和代理商用户角色，不包含费用、结算、多级代理商或技术 Agent。

除 `/api/health`、静态页面和登录接口外，API 需要认证。首个管理员可由 `WEB_USERNAME` / `WEB_PASSWORD` 环境变量 bootstrap；登录后使用 `Authorization: Bearer <token>`。配置了环境变量后，Basic Auth 仍作为管理员兼容入口。代理商用户只能访问自身 `agent_id` 下的证书和应用，不能提交 `rootpass`、`force` 或覆盖归属。

## 服务与 CA

`GET /api/health` 返回不含证书数量或文件路径的服务状态，例如 `{ "status": "ok", "version": "CornCert/1.0" }`。

## 身份与代理商

`POST /api/auth/login`：`{ "username": "admin", "password": "..." }`，返回 `token`、`expires_at` 和 `user`。

`GET /api/auth/me`：返回当前身份、角色和代理商归属。

`POST /api/auth/logout`：使当前 Bearer 会话失效。

`GET /api/agents`、`POST /api/agents`：管理员列出/创建代理商。

`PATCH /api/agents/{id}`：管理员启用或停用代理商；停用会立即使其用户会话失效。
`GET /api/users`、`POST /api/users`、`PATCH /api/users/{id}`：管理员管理代理商用户和状态。

`GET /api/ca` 返回根 CA 状态。兼容实现也可以使用 `GET /api/roots`。

`POST /api/ca/initialize` 初始化 RSA/EC 根 CA。重复调用应返回已有 CA 或 409；请求体可为空对象，根密码等运行参数通过服务启动环境提供。

## 证书

`GET /api/certificates` 返回当前身份可见的证书。管理员可传 `?agent_id=2` 筛选代理商；代理商不能指定其他归属。每项至少包含 `id`、`name`、`type`（`client` 或 `server`）、`agent_id`、`key_type`、`created_at` 和 `expires_at`。

`POST /api/certificates` 生成证书。管理员可传 `agent_id` 选择归属，代理商归属由登录身份决定。请求示例：

```json
{
  "type": "server",
  "name": "api.example.dev",
  "sans": ["api.example.dev", "10.0.0.10"],
  "days": 398,
  "key_type": "rsa"
}
```

`type` 必填，可选 `server`、`client`；`name` 必填；服务器证书可通过 `sans` 传入多个域名或 IP。`name` 会作为 Common Name 并与显式 `sans` 合并去重；`days` 和 `key_type` 可选，`key_type` 可取 `rsa`、`ec` 或 `both`，默认 `both`。不同代理商即使使用相同名称也会使用独立目录和私钥。成功响应为新证书记录（通常为 HTTP 201）。

`GET /api/certificates/{id}` 返回单个证书及其文件清单。

`GET /api/certificates/{id}/download` 下载该证书的 ZIP 包。若实现支持按格式下载，可传 `?format=crt|key|p12|pem|bundle`。

`GET /api/certificates/{id}/files/{filename}` 下载 ZIP 包中的单个文件。`filename` 必须来自证书详情返回的文件清单，服务端不得接受任意路径。

## 应用登记

`GET /api/registrations` 返回应用或容器登记信息；`POST /api/registrations` 新增登记：

```json
{ "name": "orders-api", "owner": "platform-team", "notes": "开发环境" }
```

登记记录带有 `agent_id`，代理商只能访问自身记录；不会触发证书签发或吊销。

## 兼容路径

为了方便旧客户端，服务可以同时暴露 `/api/v1/certificates` 作为 `/api/certificates` 的别名，但仍执行相同的身份和归属校验。管理员仍可直接运行 `gen_root_cert.sh`、`gen_client_cert.sh` 和 `gen_server_cert.sh`；代理商 CLI 使用 `scripts/certctl.py` 的 Bearer 登录流程。所有签发入口共享单机 CA 锁，输出文件登记到代理商隔离目录。
