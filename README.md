# holographic-zh

Hermes Agent 本地记忆插件 —— 针对[Holographic 记忆库](https://github.com/NousResearch/hermes-agent)的 **CJK（中文等无空格语言）检索零召回缺陷**修复版。保持上游数据契约不变（同一 `memory_store.db` schema、同一 `fact_store` 工具接口），可从上游 `holographic` provider 无损切换。

**[English README](README_EN.md)**

## 背景：为什么需要这个插件

### 上游缺陷（修改的由来）

Hermes 内置的 `holographic` 记忆 provider 把事实（fact）存在 SQLite FTS5 里，用的是默认 `unicode61` 分词器。这个分词器**按空白切词**——一段没有空格的中文会被当成**整整一个 token** 存进去。于是：

- 一条中文 fact 的正文（比如「PCB供应商账期口径：月结45天…」）在索引里是**一整个长 token**；
- 用户的自然中文提问（比如「云洲的账期是多少」）在查询侧也是**一整个长 token**；
- 两者永远不相等 → **检索命中数为 0**。

这不只影响手动 `fact_store(search)`：每轮自动注入（prefetch）传的就是用户原始中文句子，同样一无所获。此外 `probe` / `reason` 的 HRR 向量编码同样按空格切词，中文整句只有一个有效向量分量，对所有实体返回几乎相同的分数，无区分度。**结果是：在默认安装下，整个中文语系等于没有长期记忆。**（上游 issue：[#85524](https://github.com/NousResearch/hermes-agent/issues/85524)、[#83593](https://github.com/NousResearch/hermes-agent/issues/83593)、[#73868](https://github.com/NousResearch/hermes-agent/issues/73868)）

2026 年 10 月上游把 `holographic` provider 从核心树移出（独立仓库 [NousResearch/hermes-plugin-holographic](https://github.com/NousResearch/hermes-plugin-holographic)），该独立仓库目前无人维护，**同样不含本修复**。

### 本插件的修复

在 `facts.search_text` 列里存一份**共享的 CJK 二元组（bigram）词汤**：

- **索引侧**：中文串按 2 字滑窗切开（「供应商账期」→ `供应 应商 商账 账期`），拉丁词原样保留，空格拼接（让 unicode61 把每个 bigram 当独立 token）；
- **查询侧**：用**同一个分词器**展开查询——查询和索引产出相同的 bigram，于是**任意 ≥2 字的中文子串，无论有无空格、整句还是关键词，都能命中**；
- **透明迁移**：打开旧 schema 数据库时自动补 `search_text` 列、回填 bigram、重建 FTS 索引，无需手工操作。

同时修复 `fact_store` 更新链的两个缺陷（原核心树版在 PR review 中被发现）：

1. `update_fact(fact_id, tags=...)`（只改标签）对 `None` 调 `.strip()` 直接抛 `AttributeError`——重新打标这条常规路径完全不可用；
2. 只改 `content` 时 `search_text` 只按新正文重建，**静默丢掉标签的 bigram**——改完内容后按原标签查询即 0 命中。

现在 `update_fact` 从数据库现有行回填有效值再重建 `search_text`，tags-only / content-only / trust-only 三种局部更新都安全（`tests/test_store_update.py` 4 项回归测试覆盖）。

另外 `retrieval_count` 列上游只声明、从不写入（[#78801](https://github.com/NousResearch/hermes-agent/issues/78801)）；本插件让每次真实检索为命中条目 +1，可作为「哪些事实真的被用起来了」的统计依据。

## 与其他项目的关系

- **Hermes 核心内置 `holographic` provider**：2026-10 已从核心树移除。本插件保留其数据契约与接口，是它的无中断中文修复替代品（曾经的核心树修复 PR [#127339](https://github.com/NousResearch/hermes-agent/pull/127339) 因代码移除无合并目标而关闭，本插件即其最终形态）。
- **NousResearch/hermes-plugin-holographic**：官方独立版，无人维护，不含 CJK 修复。
- **Songokou1983/holographic-zh**：社区同名项目（jieba 分词方案，2026-04 起停更），与本插件无代码关系。

## 功能清单

- CJK bigram 索引 + 同分词器查询展开：整句中文 == 空格关键词，召回等效
- 旧数据库透明迁移（首次打开自动补列、回填 bigram、重建 FTS）
- `update_fact` 局部更新安全（tags-only / content-only / trust-only）
- `retrieval_count` 真实自增
- **分级时效衰减（0.2.0）**：按事实的 T 级标签做逐条时间衰减（如 `T0`事件永不衰减、`T3`快变 60 天半衰期），支持 `facts.ttl_days` 逐条覆盖；检索强化让常被召回的事实衰减更慢
- **版本链（0.3.0）**：`supersede` 退役过时事实（保留历史 + 死亡笔记），检索自动排除已退役条目；`timeline` 重建同主题完整版本链（旧→新），历史轨迹变成可查证据
- numpy 缺席时 HRR 向量层自动降级（权重重分配），中文关键词检索不受影响
- 纯本地：不调 embedding API、零出网调用

## 环境要求

| 依赖 | 必需性 | 说明 |
|---|---|---|
| Hermes Agent | 必需 | 本插件是 memory provider，不是独立程序（`import agent.memory_provider` 等核心模块） |
| Python ≥ 3.11 | 必需 | |
| SQLite FTS5 | 必需 | Python 官方构建与主流 Linux 发行版默认自带 |
| numpy ≥1.26, <3 | 运行时可选 | `pyproject.toml` 已声明，`plugins enable` 时自动安装；没有它 HRR 层关闭、bigram 检索照常（测试套件专门覆盖无 numpy 环境） |
| pytest + ruamel.yaml | 仅跑测试需要 | 运行时不需要 |

## 安装

前提：已安装 Hermes Agent。插件要放在**目标 profile 的** `plugins/` 目录（default = `~/.hermes/plugins/`，其它 profile = `~/.hermes/profiles/<名>/plugins/`）。

```bash
git clone https://github.com/Allanchao87/hermes-plugin-holographic-cjk.git \
  ~/.hermes/plugins/holographic-zh

hermes plugins validate ~/.hermes/plugins/holographic-zh
hermes plugins enable holographic-zh          # 自动安装 numpy 依赖
hermes config set memory.provider holographic-zh
hermes memory status                          # 应显示 Provider: holographic-zh ← active
```

生产部署建议把 Git URL 钉在不可变提交上：

```bash
hermes plugins install Allanchao87/hermes-plugin-holographic-cjk --ref <40位commit-sha>
hermes plugins enable holographic-zh
```

不要用会移动的分支做生产部署。

## 配置项

`$HERMES_HOME/config.yaml` 的 `plugins.hermes-memory-store` 段：

| 键 | 默认 | 说明 |
|---|---|---|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite 路径；写 `$HERMES_HOME` 占位符可随 profile 自动解析 |
| `auto_extract` | `false` | 会话结束时自动抽取事实 |
| `default_trust` | `0.5` | 新事实初始信任分 |
| `min_trust_threshold` | `0.3` | 检索的最低信任过滤 |
| `temporal_decay_half_life` | `0` | 全局兜底衰减半衰期（天），仅对无 T 级标签的事实生效，0 = 不衰减 |
| `ttl_map` | `{}` | 逐条衰减半衰期映射，按事实 tags 里的 T 级 token 取值，JSON：`{"T0":0,"T1":0,"T2":180,"T3":60}`（0 = 永不衰减）。例：`hermes config set plugins.hermes-memory-store.ttl_map '{"T0":0,"T1":0,"T2":180,"T3":60}' --force` |
| `temporal_reinforcement` | `true` | 检索强化：有效半衰期随 `retrieval_count` 拉长（× (1 + 0.5·min(命中数, 4))），常被召回的事实衰减更慢 |
| `hrr_dim` | `1024` | HRR 向量维度 |
| `hrr_weight` | `0.3` | HRR 相似度权重（numpy 缺席时自动重分配） |

### 分级时效衰减用法（0.2.0）

写事实时在 tags 里带时效等级：`T0` 事件型（永不失效，如清退/裁定/会晤结论）、`T1` 结构型（工具命令/表结构/口径，不衰减）、`T2` 慢变（组织人事/账期/供应格局）、`T3` 快变（行情/产能/价格快照）。配合 `ttl_map` 配置后：

- 衰减只降排序权重，**不删除**——过时事实仍可检索，只是不再顶替新快照注入
- `facts.ttl_days` 可对单条事实显式覆盖：工具层 `fact_store(action='add', ttl_days=7)` 或 `action='update', ttl_days=0`（0 = 清除覆盖、回到标签推导）；优先级 = ttl_days > tags T 级 > 全局兜底
- 事件/状态混合的事实建议拆两条：事件型记录事实、状态型记录当前快照（新快照 add 不覆盖，形成时间线）

### 版本链：supersede / timeline（0.3.0）

快变事实更新时的标准流程是「新增快照 + 退役旧快照」，不覆盖、不删除：

```
fact_store(action='add', content='澳弘价格快照（2026-12）：双面板 760 元/㎡ …', tags='澳弘 价格 T3')
  → fact_id=90
fact_store(action='supersede', fact_id=79, by_fact_id=90, reason='2026-12 调价，两个月一调到期')
  → #79 打上 valid_until + superseded_by=90 + reason，检索不再返回，历史保留
fact_store(action='timeline', fact_id=79)
  → 完整链：#79（已退役，2026-10-16 口径）→ #90（现行）+ 各自 reason
```

- `valid_until` 记录事实在现实世界失效的时刻（可显式传，默认当前 UTC），与 `updated_at`（系统时间）分开——双时间线
- 检索/注入默认排除已退役条目：旧快照不会顶替新快照，但 `timeline` 随时可查完整轨迹
- 守卫：不自指、不重复打标（链保持线性）、不能用已退役事实做替代者

## 运行测试

> pytest 9 会为带 `__init__.py` 的插件根建 Package 节点并按目录名（含连字符）导入，导致相对导入崩、22 个测试全 ERROR。必须把 rootdir 限制在 `tests/`：

```bash
cd tests && env -u PYTHONPATH uv run --with pytest --with numpy --with ruamel.yaml \
  -m pytest . -q --rootdir=. -p no:cacheprovider
# 期望：22 passed
```

## 从上游 holographic 切换（备份 / 迁移 / 回滚）

先在临时 profile 或数据库副本上验证，再动生产库。生产库不要同时被两个 provider 打开。切 provider **不移动、不改写数据库**，首次打开自动迁移。

```bash
# 1. 一致性备份（活库用 sqlite3 .backup，不要直接 cp 主 .db 文件）
hermes gateway stop
mkdir -p "$HOME/.hermes/backups"
backup="$HOME/.hermes/backups/memory_store-before-holographic-zh-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$HOME/.hermes/memory_store.db" ".backup '$backup'"
cp "$HOME/.hermes/config.yaml" "$HOME/.hermes/backups/config-before-switch-$(date +%Y%m%d-%H%M%S).yaml"

# 2. 校验
hermes plugins validate "$HOME/.hermes/plugins/holographic-zh"
hermes plugins doctor holographic-zh

# 3. 切换
hermes config set memory.provider holographic-zh
hermes memory status
hermes gateway start
hermes memory status    # 事实条数应与切换前一致，且 sqlite3 PRAGMA integrity_check 通过
```

回滚：

```bash
hermes gateway stop
hermes config set memory.provider holographic
hermes gateway start
hermes memory status
```

如需恢复备份库：先停 gateway 再拷贝（不要只拷主 .db 文件去覆盖活着的 WAL 库）。

## 已知局限与后续方向

- **单字中文查询不命中**：bigram 最小粒度 2 字，`search("账")` 返回空（与「没存」不可区分）。
- **排序无 idf / 长度惩罚**：宽召回没问题，但与提问无关的长条目也可能排进来；大库建议配合 tags 提精度（tags 与正文共用同一 bigram 体系，含连字符的拉丁词请拆开写，如 `lark-cli` → `lark cli`）。
- **无语义层**：同义/跨语言匹配（「识图」vs「OCR」）靠 bigram 命中不了；本地 embedding 语义层是后续方向。
- 未做 AND-first 精度排序、原生 `cjk_unicode61` FTS5 tokenizer（需编译 so）。

## 隐私

纯本地运行：不调 embedding API、不向外部服务发送任何事实内容。numpy 仅用于本地 HRR 向量运算。

## License

[MIT](LICENSE)
