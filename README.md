# 数字艺术授权谱系

服务于数字笔墨展览资产与授权。围绕灵境艺术馆将祝允明《草书诗帖》拆分为四款
限量数字作品、带独立编号的仿制版与可免费领取的诗句版本这一场景，系统记录
原作来源、采集批次、局部裁切、衍生关系、发行数量与各方贡献，并对出版、馆藏、
创作、展映四类分属不同机构的权利做组合判定。

## 解决的问题

* **谱系可反查**：由任一藏品编号可回溯「原作 → 采集批次 → 局部裁切 → 数字作品
  → 系列 → 该编号」的完整链条与各方贡献。
* **许可组合判定**：一份许可按地域、媒介、用途、期限组合授权；发行前综合所需的
  出版/馆藏/创作权，展映前综合展映/馆藏/创作权。生效、到期、撤销、争议、
  转让待定各时点的状态都参与判断。
* **证据快照**：每次授权决定都把当时的许可记录深拷贝并固化内容散列。事后许可
  到期、撤销或权利人变更，不会改写决定做出时的结论。
* **名额与幂等**：只有所需许可齐备时才在同一把锁内完成判定、占号、生成藏品；
  支付或链上回调凭幂等键重试，永远返回首次结果，绝不多生成藏品、不多占名额。
* **展映不可改写**：展映安排即固化授权快照；展示素材可换版（旧版本保留并标记
  失效），但展映一旦发生即冻结，换素材被拒绝。
* **决定链防篡改**：所有决定只追加进哈希链（每条含上一条散列），任何删除、
  插入、改写都能被 `/api/chain` 校验发现。
* **对外脱敏**：公众摘要只给真伪、当前权利状态与可验证散列，不含合同编号、
  备注、权利方身份等内部合同内容。

## 模块

| 文件 | 职责 |
| --- | --- |
| `domain.py` | 权利/角色、谱系节点、系列、贡献、衍生关系与散列工具 |
| `registry.py` | 线程安全存储、谱系图、哈希链式决策日志、幂等表、序列号 |
| `licensing.py` | 地域/媒介/用途/期限组合判定，生命周期状态，证据快照 |
| `application.py` | 登记、许可生命周期、名额锁定、幂等发行、展映冻结 |
| `provenance.py` | 馆方完整谱系反查与对外脱敏摘要 |
| `scenario.py` | 《草书诗帖》演示场景播种（机构、谱系、六系列、许可） |
| `service.py` | HTTP 入口，保留稳定的 `/health` 身份检查 |

## 运行

```bash
python3 service.py --check          # 基础配置与哈希链自检
python3 service.py --demo --port 8000   # 播种演示场景后启动
curl http://127.0.0.1:8000/health
```

零第三方依赖，仅用 Python 标准库。

## 主要接口

除 `/health` 与 GET 外均为 `POST` JSON：

```
GET  /api/series                                系列与剩余名额
POST /api/evaluate                              试判许可（不占名额）
POST /api/reservations                          许可齐备才锁定名额
POST /api/reservations/{id}/confirm             凭幂等键确认锁位发行
POST /api/editions/issue                        原子发行（幂等键去重）
POST /api/editions/{id}/transfer                藏品让渡（只追加）
POST /api/screenings                            安排展映（固化授权快照）
POST /api/screenings/{id}/material              展映素材换版（发生后 409）
POST /api/screenings/{id}/occur                 标记展映发生
GET  /api/editions/{id}/lineage                 馆方完整谱系反查
GET  /api/editions/{id}/summary                 对外真伪与权利状态摘要
GET  /api/screenings/{id}/summary               对外展映状态摘要
GET  /api/decisions?about=<编号或节点>           相关授权决定
GET  /api/chain                                 哈希链校验
```

发行示例：

```bash
curl -X POST http://127.0.0.1:8000/api/editions/issue \
  -H 'Content-Type: application/json' \
  -d '{"series_id":"LIM_1","recipient":"收藏家甲",
       "idempotency_key":"chain-cb-77",
       "region":"CN","medium":"digital","purpose":"distribution"}'
```

许可不齐（如海外地域、错误媒介、争议或转让待定期间）返回 `422
license_incomplete` 且不消耗名额；同一 `idempotency_key` 重试返回 `200` 与
首次生成的同一编号。

## 测试

```bash
npm test            # 等价于 python3 -m unittest discover -p 'test_*.py'
```

覆盖许可组合与生命周期、快照深拷贝、哈希链防篡改、售罄、20 路并发同键只生成
一件、不同键并发序号严格连续、两段式锁位、展映换版与冻结、谱系反查及对外
脱敏、HTTP 端到端集成。
