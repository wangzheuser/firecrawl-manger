# SearXNG 兼容门面（可选，默认关闭）

> 目标调用方：把「联网搜索」配置为 **SearXNG** 的客户端，例如 [Hob](https://github.com/ffattiger/hob)。
> 这类客户端只接受一个 `baseUrl`，并且把服务地址写死在代码里，无法指向自建网关。
> FCAM 通过暴露一个 SearXNG 形状的只读接口，让它们复用 FCAM 的 Key 池与治理能力。

## 1. 它是什么

一个 **GET** 接口，把 SearXNG 的搜索请求翻译成一次 Firecrawl `/v1/search` 调用：

```
GET /searxng/search?q=<query>&format=json&safesearch=0
Authorization: Bearer <Client Token>
```

内部走 `Forwarder.forward(provider="firecrawl")`，因此**完全复用**：

- Key 池选择、轮换与故障转移
- Client 并发、速率限制、每日配额
- 失败冷却、重试、请求日志与审计

它不新增任何上游客户端，也不改变 Firecrawl / Exa 通道的行为。

## 2. 开启方式

`config.yaml`：

```yaml
providers:
  searxng:
    enabled: true            # 默认 false
    result_limit: 10         # 单次返回条数（1-50，超出会被夹紧）
    expose_root_search: false # 是否额外挂一个裸 /search
```

默认 `enabled: false`，**关闭时路由表与升级前完全一致**（访问会得到 404）。

## 3. 接口契约

### 请求

| 参数 | 位置 | 必填 | 说明 |
|---|---|---|---|
| `q` | query | 是 | 搜索词，1-2000 字符 |
| `format` | query | 否 | SearXNG 客户端会带 `json`；服务端始终返回 JSON |
| `safesearch` | query | 否 | 客户端会带 `0/1/2`；当前原样接受、不下传 |
| `Authorization` | header | 是 | `Bearer <Client Token>`（与 Firecrawl 通道同一套） |

### 响应（200）

```json
{
  "results": [
    { "title": "…", "url": "https://…", "content": "…" }
  ]
}
```

- 字段名是 SearXNG 的 `content`（来源是 Firecrawl 的 `description`）
- 没有 `url` 的条目会被丢弃
- 附加 `Cache-Control: no-store`

### 错误

| 情况 | 状态码 | 说明 |
|---|---|---|
| 未带 / 错误的 Client Token | 401 | 与其它数据面接口一致 |
| 缺少 `q` | 400 | FCAM 的 `VALIDATION_ERROR` 信封 |
| Client 并发/速率/配额受限 | 429 | 复用既有治理限制 |
| 上游 Firecrawl 报错 | 原样透传 | 保持 FCAM 的 JSON 错误信封 |
| 上游返回非 JSON | 502 | `{"results": [], "error": "..."}` |

## 4. 调用示例

```bash
curl -s "http://127.0.0.1:8000/searxng/search?q=hob&format=json&safesearch=0" \
  -H "Authorization: Bearer $FCAM_CLIENT_TOKEN"
```

## 5. 与 Hob 对接

Hob 的「设置 → 联网搜索 → SearXNG」只要求填服务地址：

- **服务地址**：`http://<fcam-host>:8000/searxng`
- **API Key**：FCAM 的 Client Token
- 启用即可，Hob 会用 `GET {baseUrl}/search?q=…&format=json&safesearch=0` 调用

> 如果客户端不接受带路径的 baseUrl，把 `expose_root_search: true` 打开，
> 然后地址填 `http://<fcam-host>:8000`（此时挂载裸 `/search`）。

## 6. 超时注意

Hob 侧搜索超时为 **15 秒**。建议把 Firecrawl 通道的超时与重试控制在总时长以内：

```yaml
firecrawl:
  timeout: 10          # 单次上游超时
  max_retries: 0       # 重试会叠加总时长
```

否则客户端会先断开，而 FCAM 仍在重试。

## 7. 兼容性说明

- 关闭（默认）：不挂载任何路由，行为与之前完全一致
- 开启：仅新增 `/searxng/search`（可选裸 `/search`），不修改任何既有路由
- 不影响 `/v1/*`、`/v2/*`、`/api/*`、`/exa/*` 的语义
- 审计日志中新增端点标识 `searxng_search`
