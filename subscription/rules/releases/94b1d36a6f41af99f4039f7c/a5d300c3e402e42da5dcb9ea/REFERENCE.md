# 规则引用

导入 `providers_mrs_no_resolve_file.yaml` 的完整 `rule-providers`，即可使用
`RULE-SET,company_github,<订阅策略>`。file 版路径相对 Mihomo 工作目录，整个
`rules_set/` 必须随包复制。HTTP 版仅在独立发布且摘要核验后使用。
normal / no_resolve、YAML / MRS 均提供完整 provider 片段。classical 命名的兼容接口
使用原生 YAML domain/ipcidr 表和残余 classical，保持大小写及混合分段语义。
原始 classical 输入也保留，但固定内核直接加载其中 DOMAIN 时对请求大小写敏感；
请使用配套逻辑接口，不要把原始输入当作等价的直接引用片段。不要只复制逻辑
inline provider：底层分段依赖不可省略。缓存路径按名称、格式和 URL 区分。

全部明确业务域名（含专用服务及直连域名）先引用，所有进程其次、核验网络随后、
通用 proxy 兜底最后。Netflix、Telegram 等也有独立 _process / _network 逻辑名。
广告规则有意优先，
独立管理主机例外见公司清单。兼容产品视图只属于其 canonical 公司，不应另绑冲突策略。
`copilot` 仅表示 Microsoft Copilot；GitHub Copilot 属于 `company_github`。

GitHub 需共同引用 `company_microsoft`（default.exp-tas.com、vscode.dev）和
`shared_dependencies`。项目实际 profile 将 company_github 显式绑定 Microsoft，
保持默认跟随；订阅可以修改该绑定，规则包不会替订阅创建或同步选择组。
OpenAI、Microsoft 也需要 shared_dependencies；JetBrains 的 npm 依赖归 GitHub。
共享依赖只有一份，不宣称独立切换后所有认证/第三方依赖都自动共用出口。

腾讯 CodeBuddy（国内 copilot.tencent.com、国际 codebuddy.ai）的专属服务和资源归
service_codebuddy；腾讯统一身份、微信登录及验证码归 shared_tencent，二者共同引用。
不把 CodeBuddy 归入 Microsoft/GitHub Copilot。JetBrains 自有域归 service_jetbrains。
具体绑定由订阅决定；本项目 CodeBuddy/腾讯共享身份绑定全球直连，JetBrains 绑定 Proxy。
兼容产品视图需要共同引用其 canonical 分类，使用相同策略；其中保留的已审核
进程/网络语义标记为 compatibility，现代订阅使用分层逻辑名。
完整依赖、逻辑成员、兼容视图及引用顺序见 reference_manifest.json。
过滤、待审、迁移及有限排除见 normalization.json；原始输入摘要见 input_manifest.json。
新正文或 DLC 文件变更不自动取得审核资格，需复核 input_review.json 及准入决定。
覆盖退化复核与逐项待审处置见 coverage_review.json。

发布整包放在 releases/<snapshot>/<bundle_digest>/，精确路径见发布收据 published.json；
物理 provider 使用带快照的 mrs/、yaml/ URL；原生 classical 形状的 rules*.yaml
索引也只指向 classical/<snapshot>/，保留其原生大小写语义，不指向旧固定地址。
旧 classical 固定 URL、旧根目录 provider 定义及已有快照都不会被新发布覆盖。

最小例子（占位策略需由订阅定义）：
```yaml
rules:
  - RULE-SET,company_github,GitHubPolicy
  - RULE-SET,company_microsoft,MicrosoftPolicy
  - RULE-SET,shared_dependencies,SharedPolicy
  - MATCH,FallbackPolicy
```
可加载的完整占位示例见 example_mihomo.yaml；真实订阅验收另外调用现有生成器。
