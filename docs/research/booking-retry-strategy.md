# POSS 高并发抢位的重试策略

调研日期：2026-10-08。范围：本仓库当前 `poss_client.py` / `monitor.py` 的查询、确认页和最终提交；这里只提出策略，不表示 POSS 已承诺任何幂等性或记录可见性时限。

## 结论

**查询失败可以有限重查；最终下单请求发出后，任何无响应、超时、断线或含糊的 HTTP 错误都先当作结果未知。** 查询到空位只是某一时刻的快照，不是锁定名额；查询到无空位也不能说明最终提交是否成功。RFC 9110 对非幂等方法的自动重试要求知道其语义幂等，或者能确认原请求未生效。[HTTP 幂等语义][rfc-idempotency] [Google Cloud 对响应和幂等性的双重判断][gcs-retry]

### 按阶段决策

| 阶段及仓库入口 | 无响应/可恢复错误时 | 重试前条件 |
| --- | --- | --- |
| 登录页、预订页和会话保活 `login()` / `load_booking_page()` / `heartbeat()` | 读页面可以限次重试；登录 POST 不能仅凭 HTTP 方法推断无副作用，失败时先检查会话能否访问预订页，再决定重新登录。 | 不在认证失败、CSRF 格式改变、明确禁止访问时循环尝试；保留放位截止时间。 |
| 时间表 `timetable()` → `_post_timetable()`：**POST，但按当前用途是查询** | 对连接/读超时、断连、408、429、500/502/503/504 有界重查；若页面重定向登录，刷新会话和 CSRF 后重查。HTTP 方法为 POST，故重试理由是已确认的“只读业务语义”，不是 urllib3 默认的幂等方法列表。 | 每次重新获取有效会话/令牌；对 429/503 的 `Retry-After` 遵从服务器等待要求。POSS 的只读语义目前仅由客户端用途推断，部署前应以官方页面请求与行为确认。 |
| 无空位 `availability` | 不是 HTTP 错误。放位窗口内按低频、有抖动的轮询重查；出现空位才进入下一步。 | 不因名额消失推断自己已经下单，也不并发提交同一目标。 |
| 第一步 `first_step`：POST `make_book.do` 获取确认表单 | 已拿到明确拒绝且未进入最终提交时，可根据业务原因重新查空位并重新生成确认页。读超时、断线、5xx 或响应不可解析时，**先暂停自动重发**。 | 仓库只证明“本次客户端未发最终 POST”，不能证明服务器第一步没有建立临时占位/修改状态；先核验 `My Record` 和会话/页面状态，并确认 POSS 第一步的服务端语义。只有证实未提交且重发不会重复占位，才允许有界地重新开始查询→确认流程。 |
| 最终 `final_submit`：POST 确认表单 | 无响应、`ReadTimeout`、`SSLError`、连接重置、5xx、无 `Location` 的跳转、结果页不可读：进入 `unknown`，**禁止自动再次 POST**。明确、可信的业务拒绝可终止本轮；仅凭状态码不能证明“未扣费/未建单”。 | 优先重试只读的结果页或 `My Record` 核验；若未核实，继续 `unknown` 并通知人工。只有 POSS 官方提供同一幂等键的服务端去重保证，或权威记录/接口能证明原请求未生效且不存在在途处理，才考虑重发。 |
| 结果页 GET、`My Record` GET | 可在短暂间隔后有界重读；见到与目标、账号及有效状态相符的记录才报 `booked`。 | 立即查不到记录不是“没有下单”的证明；超过核验期限、记录不可达或匹配冲突仍为 `unknown`，需人工核对。 |

这张表对第一步采取保守做法：仓库知道该 POST 返回确认表单，却没有 POSS 服务端对“临时占位”的契约。只要不能排除副作用，POST 的响应丢失就有歧义。[RFC 9110 §9.2.2][rfc-idempotency] [urllib3 对读错误的定义][urllib3-retry]

### 错误与 HTTP 状态码

- **连接建立阶段**：`requests.ConnectTimeout` 官方注明可重试；对纯查询直接受限重试。不能把所有 `ConnectionError`、`SSLError` 或笼统的 `Timeout` 都当成“请求没有送达”：这些类型本身不足以证明服务端未处理。最终 POST 若能可靠确认发生在发送前，才可能例外；无法证明时走 `unknown`。[Requests 异常类型][requests-api] [urllib3 `connect`/`read` 的区别][urllib3-retry]
- **读超时/断线/响应格式坏**：请求可能已送达。只读查询可重查；两次业务 POST 要按上表先核验。`timeout=30` 不能理解成“全流程最多 30 秒”：Requests 的单值分别用于连接和读取；读取超时是两次收到字节间的等待，不是整包下载或端到端截止时间。[Requests 超时说明][requests-timeout] [Requests Quickstart][requests-quickstart]
- **408、429、500/502/503/504**：查询通常可有界重试；408 表示服务器未及时收完整请求，429 表示限流，503 表示可能暂时过载，502/504 涉及上游响应。它们**不是最终下单可重发的白名单**：代理或应用出错前后，后端仍可能已处理。429 的 `Retry-After` 和 503 的 `Retry-After` 应先于本地抢位频率考虑；超过本轮时间预算就停止，而非提前重试。[RFC 9110 状态定义及 `Retry-After`][rfc-status] [RFC 6585 §4][rfc-429]
- **400、401/403、404、409、422 等**：不要作为网络瞬断盲重试。解析明确的业务错误；会话失效只在确认是登录页/认证问题后重登一次，再刷新令牌；403 不重复用相同凭据冲击服务；409 要按冲突内容重新查状态。`slot_missing`、缺 CSRF、表单变化属于诊断/适配问题。[RFC 9110 4xx 定义][rfc-status]
- **3xx**：按 POSS 预期的同站内结果跳转读取 GET，不能把跳转误判成失败后重发 POST；缺失或异常跳转后核验记录。当前客户端对提交 POST 已设 `allow_redirects=False`，手动读取结果页。
- **明确业务拒绝**：只有响应内容与 POSS 业务规则相符、足以判定未建单，才可重新进入“查空位”流程；从 `500` 页面或含糊的成功/失败字样推不出这一结论。

### 节奏、连接与总预算

1. 一个目标同一时间至多一个未决下单流程；把重试放在 `monitor.py` 的目标级决策点，避免在 `Retry`、客户端方法和外层循环叠加放大请求。设置单调时钟的整个目标截止时间、单次请求超时及最大查询次数；到期停止发新请求，但若最终提交已发出，仍应保留核验路径。Google 的重试指南把响应、幂等性和重试预算一起考虑。[Google Cloud 重试策略][gcs-retry]
2. 对**可重试的查询错误**，可从 `uniform(0, min(4s, 0.5s × 2^(n-1)))` 的完整抖动开始试点（`n=1` 为首次失败），并设最大次数及总时间上限。这些数值是本项目待测起点，不是 POSS 的官方限额。**无空位轮询**单独从现有约 3 秒间隔加少量随机扰动开始，避免同刻再撞；遇 429、503 或服务器的 `Retry-After` 时降速并服从它。固定 3 秒以及立即无限重试都可能在拥堵时制造同步流量。AWS 的原始实验说明指数退避加随机抖动可降低请求聚集；urllib3 自带的 `backoff_jitter` 是“指数值加随机值”，不等同这里的完整抖动。[AWS 退避与抖动实验][aws-jitter] [urllib3 `Retry` 参数][urllib3-retry]
3. 现有 `PossClient` 共用一个 `requests.Session()`，会保留 Cookie 并复用连接；单线程顺序抢位目前无需扩大连接池。先实测连接/首字节耗时，再为**查询**试点 `(connect, read)=(3.05, 8)` 秒，为**最终提交及核验读取**选择能覆盖 POSS 正常慢响应的更长 `read` 值（例如 20–30 秒）；超时过短会把正常慢响应变成更多歧义。这些是试验值，当前代码全用单值 `timeout=30`。即便采用二元组，也要用单调时钟约束整个流程；多 IP 地址连接可能让实际等待超过单次 connect 值。[Requests Session 与连接池][requests-api] [Requests 超时说明][requests-timeout]
4. `HTTPAdapter` 默认 `max_retries=0`，Requests `Session` 已有连接池；不要为了“抢得快”直接设置全局 `Retry(allowed_methods=None)` 或把最终 POST 放进状态码重试表。若以后只对查询 POST 使用适配器，也必须隔离最终提交路径；因为它们同属一个站点，共享 URL 前缀的自动重试策略很容易误伤。更直观的是针对查询方法做显式重试，并记录每次尝试。仓库当前 `requirements.txt` 写的是 `requests>=2.31.0`，没有锁定 Requests/urllib3 版本；若实现依赖 `backoff_jitter` 等具体参数，应先锁版本并核对该版本 API。[Requests `HTTPAdapter` 默认值][requests-api] [urllib3 重试规则][urllib3-retry]
5. `Retry-After` 可以是秒数或 HTTP 日期；超过目标截止时间就终止该轮，不能在 120 秒窗口内“截短”服务器给出的等待后抢跑。持续证书验证失败、账号/权限错误或接口结构改变不适用拥堵退避；记录分类后停止自动尝试。[RFC 9110 `Retry-After`][rfc-status] [Requests TLS 验证默认值][requests-api]

### 未知结果与幂等

`My Record` 是当前项目可用的核验路径，但它只能提供客户端观察：记录查询也可能暂时失败或延迟可见，当前代码只按中心、日期、时段及确认状态匹配一行。实施前核对是否能加入用户、设施、预订编号、状态及费用等更强的唯一标识；第一次查不到只安排有界重读，不把“未查到”当成下单失败。空位是否消失同样不能判断是谁下的单。

若 POSS 没有**服务端承认的**幂等键和去重契约，客户端自行生成 UUID、`Idempotency-Key` 头或在本地 `state.json` 标记都不能让最终 POST 幂等。真正安全的自动重发需要服务端把相同用户/请求标识的重复提交原子地识别为同一笔，并限定有效期、参数一致性；AWS 的一手设计文档详细说明了这个条件。当前仓库没有证据表明 POSS 支持该契约。[AWS 幂等 API 设计][aws-idempotency] [RFC 9110 §9.2.2][rfc-idempotency]

建议显式状态机：`pre_submit` → `final_sent` → `booked | rejected | unknown`。**在发送最终 POST 前**持久化目标唯一标识和尝试时间；收到结果后再更新。进程被杀、超时或重启时，持久化的 `final_sent/unknown` 只允许进入只读核验，不直接重放 POST。唯一标识应包含账号、日期、起止时间、中心、设施、活动及本次意图；与当前 `monitor.py` 的 `centerId|date|from` 去重键相比更不易混淆目标。记录阶段、异常类型、HTTP 状态、`Retry-After`、耗时及核验次数，不写凭据、Cookie、CSRF 或原始私密页面。

## 与现有实现的差距及执行顺序

1. **优先修正结果分类和核验**：`poss_client.py` 在 `first_step` 发送后异常时标成 `failed`，虽然最终 POST 未发，仍缺第一步是否有副作用的依据；应区分“肯定未发出”和“已发送但响应未知”。`final_submit`/结果页/`verify_record` 异常保守地进入 `unknown` 是正确方向；对 `unverified_result` 做有限的 `My Record` 重读。`monitor.py` 已把 `unknown` 写入 `processed` 并阻止正常运行再次下单，应在重启后提供只读核验入口和人工状态说明。
2. **随后改善查询恢复**：`monitor.py` 只在 `availability/timetable` 的 `no_vacancy/network_error` 失败时按固定 `retry_interval` 进入下一轮；`_post_timetable()` 的非 200、坏 JSON 和服务器错误信息当前不会进入这个集合。按上述分类加入有界重查、尊重 `Retry-After`、刷新过期会话/CSRF；不要把 `TimetableError` 全部当瞬时故障。
3. **最后用观测值调参**：仓库已有分阶段耗时和请求事件；在不泄漏敏感数据的前提下补充 `ConnectTimeout`/`ReadTimeout`/TLS 分类、状态码、是否发出最终 POST、等待来源和剩余预算。用多次正常访问的延迟分布决定 timeout 和退避参数；不要把 08:30 的拥堵猜测当成 POSS 的已知服务能力。

本调研未调用 POSS 下单接口，也没有拿到 POSS 官方关于第一步占位、幂等键、订单可见延迟或限流阈值的契约；涉及这些语义的安全判断刻意保持保守。文中数值均为待观测的工程起点。

## 一手来源

[rfc-idempotency]: https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2
[rfc-status]: https://www.rfc-editor.org/rfc/rfc9110.html#section-15
[rfc-429]: https://www.rfc-editor.org/rfc/rfc6585.html#section-4
[requests-api]: https://requests.readthedocs.io/en/stable/api/
[requests-timeout]: https://requests.readthedocs.io/en/stable/user/advanced/#timeouts
[requests-quickstart]: https://requests.readthedocs.io/en/stable/user/quickstart/#timeouts
[urllib3-retry]: https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Retry
[gcs-retry]: https://docs.cloud.google.com/storage/docs/retry-strategy
[aws-jitter]: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
[aws-idempotency]: https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
