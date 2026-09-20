# 在线证书管理 API

服务默认监听本机地址（具体端口由启动参数决定），接口以 `/api` 为前缀。所有请求和响应使用 JSON；下载接口返回文件流。当前版本用于单机、内网集中登记，暂不包含认证和吊销。

## 服务与 CA

`GET /api/health` 返回服务状态，例如 `{ "status": "ok", "version": "1.0" }`。

`GET /api/ca` 返回根 CA 状态。兼容实现也可以使用 `GET /api/roots`。

`POST /api/ca/initialize` 初始化 RSA/EC 根 CA。重复调用应返回已有 CA 或 409；请求体可为空对象，根密码等运行参数通过服务启动环境提供。

## 证书

`GET /api/certificates` 返回已登记证书数组。每项至少包含 `id`、`name`、`type`（`client` 或 `server`）、`key_type`、`created_at` 和 `expires_at`。

`POST /api/certificates` 生成证书。请求示例：

```json
{
  "type": "server",
  "name": "api.example.dev",
  "sans": ["api.example.dev", "10.0.0.10"],
  "days": 398,
  "key_type": "rsa"
}
```

`type` 必填，可选 `server`、`client`；`name` 必填；服务器证书可通过 `sans` 传入多个域名或 IP。服务端脚本会把 `name` 作为 Common Name、输出目录和第一个 SAN，并把它与显式 `sans` 合并去重；`days` 和 `key_type` 可选，`key_type` 可取 `rsa`、`ec` 或 `both`，默认 `both`（与 CLI 一样同时提供两套密钥）。成功响应为新证书记录（通常为 HTTP 201）。

`GET /api/certificates/{id}` 返回单个证书及其文件清单。

`GET /api/certificates/{id}/download` 下载该证书的 ZIP 包。若实现支持按格式下载，可传 `?format=crt|key|p12|pem|bundle`。

`GET /api/certificates/{id}/files/{filename}` 下载 ZIP 包中的单个文件。`filename` 必须来自证书详情返回的文件清单，服务端不得接受任意路径。

## 应用登记

`GET /api/registrations` 返回应用或容器登记信息；`POST /api/registrations` 新增登记：

```json
{ "name": "orders-api", "owner": "platform-team", "notes": "开发环境" }
```

登记记录用于跨应用追踪证书归属，不会触发证书签发或吊销。

## 兼容路径

为了方便旧客户端，服务可以同时暴露 `/api/v1/certificates` 作为 `/api/certificates` 的别名。CLI 仍可直接运行 `gen_client_cert.sh` 和 `gen_server_cert.sh`；在线接口调用相同的 OpenSSL 生成流程，并把输出文件登记到证书目录。
