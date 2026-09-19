# 数字艺术授权谱系

本项目服务于数字笔墨展览资产与授权。原作、衍生素材、创作者贡献和展映许可要形成清晰的来源关系。 系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

运行 `python3 service.py --check` 可检查基础配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可以确认服务身份。

## 领域能力

- **来源谱系**：登记原作、采集批次、局部裁切、衍生父子关系、发行数量与各方贡献；任意藏品编号可经 `GET /lineage/<编号>` 反查完整谱系与每一次授权决定。
- **许可评估**：许可按地域、媒介、用途、期限组合判断；转让、到期、撤销与争议期间的决定均写入仅追加的证据日志（哈希链 + 状态快照）。
- **发行保护**：所需许可齐备才锁定发行名额（`POST /editions/<id>/lock`）；支付与链上回调按幂等键去重，重试不会多生成藏品，发行量不超过名额。
- **展映不可改写**：替换展示素材只新增版本，已发生的展映记录保持原版本与内容哈希不变。
- **对外验证**：`GET /public/digest/<编号>` 返回可校验真伪与当前权利状态的摘要，不泄露内部合同与持有人身份。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/works` `/batches` `/assets` `/contributions` | 登记原作、批次、资产、贡献 |
| POST | `/licenses`、`/licenses/<id>/revoke` | 登记与撤销许可 |
| POST | `/editions`、`/editions/<id>/lock` | 定义发行并锁定名额 |
| POST | `/payments/confirm`、`/chain/callbacks` | 幂等的支付与链上回调 |
| POST | `/collectibles/<编号>/transfer` | 转让（争议期间冻结） |
| POST | `/assets/<id>/display`、`/exhibitions` | 替换素材、记录展映 |
| POST | `/disputes`、`/disputes/<id>/close` | 开启与结案争议 |
| GET | `/lineage/<编号>`、`/public/digest/<编号>` | 馆内谱系反查、对外验证摘要 |

## 测试

`npm test` 运行全部契约测试（`service_contract` 与 `catalog_contract`）。
