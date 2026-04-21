# research

这是一个用于维护 Clash 规则集的自动化仓库。项目会从 GitHub 上指定的第三方规则库下载最新规则，合并并去重 `payload` 内容，然后生成本仓库自用的规则文件，方便在 Clash / Mihomo 等客户端中作为 `rule-providers` 引用。

## 项目目标

- 从 `source` 目录中声明的上游规则地址拉取最新 YAML。
- 按服务分类生成独立规则集，例如 OpenAI、Google、Netflix、Telegram、Apple、Microsoft 等。
- 同时维护普通规则和 `no-resolve` 规则版本。
- 保留少量手工维护规则，用于覆盖自动规则源没有包含的特殊场景。
- 通过 `rules` 目录输出可直接引用的规则索引和规则集文件。

## 目录结构

```text
.
├── 01.merge_rules.py          # 从 source 下载、合并并生成 rules 目录内容
├── 02.rule_weighting.py       # 对 proxy.yaml 做去重/权重整理的辅助脚本
├── source/                    # 规则源配置
│   ├── *.yaml                 # 普通规则源
│   └── no_resolve/            # no-resolve 规则源
├── rules/                     # 生成后的规则输出目录
│   ├── rules.yaml             # 普通规则索引
│   ├── rules_no_resolve.yaml  # no-resolve 规则索引
│   └── rules_set/             # 实际规则集 payload 文件
├── prompt/                    # 脚本生成需求说明
└── .tmp_rules/                # 生成过程中的临时输出目录，脚本运行后会删除
```

## 输入配置

`source` 下的每个 YAML 文件定义一个或多个规则节点。节点格式如下：

```yaml
openai:
  type: http
  behavior: classical
  urls:
    - https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/OpenAI/OpenAI.yaml
    - url: https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/openai
      format: dlc
      name: openai
  path: ./ruleset/openai.yaml
  interval: 86400
```

字段说明：

- `type`: Clash rule-provider 类型，目前主要是 `http`。
- `behavior`: 规则行为，目前主要是 `classical`。
- `urls`: 上游规则地址列表，支持字符串 URL，也支持带格式声明的对象。
- `path`: 源配置中的本地路径字段，生成时会被转换为 `rules/rules_set` 下的实际路径。
- `interval`: Clash 客户端更新间隔，默认 86400 秒。

`urls` 中的字符串会使用 `format: auto` 自动识别；对象形式支持：

- `url`: 上游地址。
- `format`: 可选值为 `auto`、`clash`、`dlc`。建议对 `domain-list-community` 使用 `dlc`，避免依赖 URL 模式猜测。
- `name`: 可选，仅 `format: dlc` 使用，对应 `domain-list-community/data` 下的文件名。URL 中能推断 `/data/{name}` 时可以省略，但建议显式填写，方便审阅。

`source/no_resolve` 的结构相同，但用于生成 `rules/rules_set/no_resolve` 和 `rules/rules_no_resolve.yaml`。

## 支持的规则源格式

### Clash YAML

来自 `blackmatrix7/ios_rule_script` 的规则通常是 Clash YAML：

```yaml
payload:
  - DOMAIN-SUFFIX,example.com
  - DOMAIN,api.example.com
```

脚本会直接读取 `payload` 并合并。

### domain-list-community

来自 `v2fly/domain-list-community` 的 `data/*` 是 geosite 文本格式：

```text
example.com
full:api.example.com
keyword:example
include:another-list
regexp:^skip-this-example$
```

脚本会转换为 Clash classical：

- 无前缀或 `domain:` -> `DOMAIN-SUFFIX`
- `full:` -> `DOMAIN`
- `keyword:` -> `DOMAIN-KEYWORD`
- `include:` -> 递归展开，并支持 `@attr` / `@-attr` 过滤
- `regexp:` -> `DOMAIN-REGEX`

脚本会把 `v2fly/domain-list-community` 通过 SSH 缓存在 `.cache/domain-list-community`。解析 `include:` 时优先读取本地缓存，避免对 GitHub raw 发起大量递归请求。第一次运行或缓存存在时会自动执行浅克隆/更新。

例如：

```text
claude.com
full:servd-anthropic-website.b-cdn.net
```

会生成：

```yaml
payload:
  - DOMAIN-SUFFIX,claude.com
  - DOMAIN,servd-anthropic-website.b-cdn.net
```

## 输出结果

运行生成脚本后会输出：

- `rules/rules_set/{name}.yaml`: 对应服务的普通规则集。
- `rules/rules_set/no_resolve/{name}.yaml`: 对应服务的 `no-resolve` 规则集。
- `rules/rules.yaml`: 普通规则集索引。
- `rules/rules_no_resolve.yaml`: `no-resolve` 规则集索引。

索引文件中的每个节点会包含可供 Clash 使用的远程 URL，例如：

```yaml
openai:
  type: http
  behavior: classical
  path: ./rules_set/openai.yaml
  interval: 86400
  url: https://raw.githubusercontent.com/darkli/research/main/rules/rules_set/openai.yaml
```

## 使用方式

先安装依赖：

```bash
pip install pyyaml requests
```

更新规则：

```bash
python 01.merge_rules.py
python 02.rule_weighting.py
```

`01.merge_rules.py` 会执行以下操作：

1. 遍历 `source` 和 `source/no_resolve`。
2. 预扫描所有规则源，去重后并发下载每个节点中 `urls` 指向的上游 YAML。
3. 自动识别 Clash YAML 或 domain-list-community 文本格式。
4. 读取并合并所有 `payload`。
5. 按原始顺序去除重复 payload。
6. 清理同一规则集内被 `DOMAIN-SUFFIX` 覆盖的冗余 `DOMAIN` / 子级 `DOMAIN-SUFFIX`。
7. 清理同一规则集内被父级 `IP-CIDR` / `IP-CIDR6` 覆盖的子网段规则。
8. 按规则类型分组排序，组内保留原始相对顺序，便于人工查看并减少无意义 diff。
9. 在 `.tmp_rules` 中生成完整临时输出。
10. 全部节点成功后，再替换自动生成的规则文件和索引文件。
11. 保留手工维护规则文件。
12. 输出 payload 数量统计。
13. 删除 `.tmp_rules` 临时目录。

如果任意上游下载或解析失败，`01.merge_rules.py` 会以非 0 状态退出，并且不会发布临时输出，避免把现有规则覆盖成空文件或半成品。

默认并发下载数是 8，可以通过环境变量调整：

```bash
RULE_DOWNLOAD_WORKERS=12 python 01.merge_rules.py
```

如果 GitHub raw 下载失败，脚本会先重试，并在前几次失败后尝试使用 jsDelivr 作为 raw 文件 fallback。生成日志中的 `Download recovered` 表示该 URL 已经通过后续重试或 fallback 成功恢复。

## 手工维护规则

部分规则不是由 `source` 自动生成，而是手工维护，例如：

- `rules/rules_set/private_direct.yaml`
- `rules/rules_set/proxy_selected.yaml`
- `rules/rules_set/uk_vowifi.yaml`

这些规则用于补充银行、指定代理服务、VoWiFi 等特殊场景。运行 `01.merge_rules.py` 不会自动生成这些文件，但后续整理规则时需要注意不要误删。

## proxy 规则整理

`02.rule_weighting.py` 的设计意图是整理 `proxy.yaml`：

1. 合并其他规则集中的所有 payload。
2. 从 `proxy.yaml` 中移除已经存在于专用规则集的 payload。
3. 让 `proxy.yaml` 只保留兜底代理规则。

该脚本会分别处理 `rules/rules_set/proxy.yaml` 和 `rules/rules_set/no_resolve/proxy.yaml`，并保持 `proxy.yaml` 原有顺序。它只读取其他规则作为排除来源，不会修改手工维护规则。

为减少大规则文件的解析开销，脚本会优先使用轻量 payload 行解析；遇到注释、引号或非标准 YAML 结构时会自动回退到 PyYAML 解析。

注意：`proxy.yaml` 是先由 `01.merge_rules.py` 从上游生成，再由 `02.rule_weighting.py` 做后处理。因此日常维护时应固定按顺序运行：

```bash
python 01.merge_rules.py
python 02.rule_weighting.py
```

## 维护流程

日常更新建议流程：

```bash
python 01.merge_rules.py
python 02.rule_weighting.py
git diff
```

检查生成结果无误后提交：

```bash
git add 01.merge_rules.py 02.rule_weighting.py source rules README.md
git commit -m "Update rules"
```

如需新增一个服务规则：

1. 在 `source` 下新增 `{name}.yaml`。
2. 如需要 `no-resolve` 版本，在 `source/no_resolve` 下新增 `{name}_no_resolve.yaml`。
3. 运行 `python 01.merge_rules.py`。
4. 检查 `rules/rules_set` 和索引文件是否生成正确。

## domain-list-community 映射策略

本项目以 `blackmatrix7/ios_rule_script` 的服务分类为基础，`domain-list-community` 只作为同类服务的补充来源。维护时建议遵守：

- 优先选择与当前文件名或服务边界一致的 `data/*` 文件，例如 `claude` 补充 `anthropic`，`youtube` 补充 `youtube`。
- 不默认加入过宽的大类，例如 `cn`、`category-ads-all`、`category-social-media-!cn`，避免生成时间过长或规则范围失控。
- 如果一个服务天然包含多个官方子服务，可以显式列出多个 DLC 文件，例如 `amazon` 当前补充 `amazon`、`amazontrust`、`aws`、`primevideo`。
- 对存在争议的映射，优先保守添加，并通过生成后的 `rules/rules_set/{name}.yaml` 检查实际域名范围。

## 注意事项

- 上游规则依赖 GitHub raw 地址，运行时需要能访问对应 URL。
- `domain-list-community` 缓存依赖 SSH 访问 GitHub，默认使用当前用户 `~/.ssh` 中可用的 key。
- 生成脚本会覆盖同名的自动生成规则文件。
- `payload` 会去重、清理包含关系并按规则类型分组，因此生成文件中的规则顺序不完全等同于上游源文件顺序。
- 手工维护规则不参与自动生成、自动排序和自动清理，会保留人工章节与注释结构。
- 手工维护规则应避免和自动生成规则混在同一个维护流程中被覆盖。
- 当前项目没有锁定 Python 依赖版本，建议在固定环境中运行更新。
