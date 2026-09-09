# Colab Daily Paper 与 crawler 共识

本文是 Colab Daily 的 crawler 研究范围和 Paper production track 共识。它供 source crawler 进行召回与方向预筛选，也供 Paper track 独立完成准入、评分、同 track 比较、cutoff 和全文 refine。这里记录当前团队偏好，不是不可修改的硬编码规则；后续应根据课题组反馈、漏检和误检持续修订。

News 与 Policy 分别读取根目录 `news_consensus.md` 和 `policy_consensus.md`，与 Paper 构成三个相互隔离的 score domain。三者的分数和排序语义互不可比较，不得归一化、加权或交叉排序。

## 使用方式

- crawler 执行前读取本文的研究范围、检索配置和来源记录要求，只负责发现候选并保存来源材料，不做最终 canonicalization、机器分类、生产评分或 final publication 写入。
- source record 中的 `category_hint`、`source_category_hint`、RSS 分类和来源名称仅用于召回与审计，不能决定 `Records.Category`。
- 共享阶段先建立完整 source inventory、完成跨来源 canonicalization、为每个 canonical object 精确赋值一次 `Category`，再做上一周期身份排除。共享阶段不使用本文执行生产 triage、评分、比较、cutoff 或 refine。
- Paper track 只接收共享 routing manifest 中已冻结为 `Category = Paper` 的对象，不重新分类；使用独立 clean context 完成 triage、评分、Paper 内比较、cutoff 和 refine，不读取 News 或 Policy 候选内容、判断、分数、顺序或产物。
- 修改本文后，应在固定测试窗口检查召回数量、明显漏检、无关结果比例和 Paper track 的边界判断；不能只根据单次运行结果调整规则。

## 共享分类边界

机器分类是跨来源 canonicalization 后对现实对象作出的单值判断，冻结为最终 record 的 `category`；只能是精确枚举 `Paper`、`News`、`Policy`。分类依据对象形态、原始内容和主要事实，而不是来源名称、来源数量或 source hint。一个 canonical object 只赋值一次，后续任何 track 或 sink 均不得改变。

- `Paper`：论文、预印本或明确以学术研究成果为主体的对象，包括 arXiv 与 Hugging Face Papers 对同一研究的多条来源记录。
- `News`：以新发生的技术、产品、研究、部署、行业或安全事实为主体的新闻、官方公告或实质性报道。
- `Policy`：以法律法规、监管、标准、政府行动、公共资助/采购或安全治理事实为主体的文件或实质性追踪。

本文后续生产规则只适用于已冻结为 `Paper` 的对象。论文的配套产品公告、机构新闻稿或政策回应如果是可区分的现实对象，应由共享阶段独立 canonicalize 和分类，而不是合入论文后改变 Paper 评分。

## 研究重点

### 一级重点：具身智能与机器人

- 具身智能（Embodied AI）、机器人学习、机器人感知、规划、控制和操作。
- 机械臂及移动操作，尤其是视觉-语言-动作（VLA）模型、机器人基础模型和通用操作策略。
- 无人机相关的 VLA、视觉-语言-导航（VLN）、空中机器人、自主飞行、导航、感知和规划。
- 空天具身智能（Aerospace Embodied AI），覆盖航空与航天场景中的机器人、智能体、世界模型与自主系统，包括在轨操作、空间机器人、卫星服务、太空抓取与装配、月球/火星探测机器人、机场与低空航空器自主作业等；此类内容属于组内优先报道方向。
- 具身基础设施（Embodied Infrastructure），包括机器人数据基础设施、仿真器、遥操作、数据采集、训练平台、部署系统、评测环境和真实世界闭环系统。
- 世界模型（World Models），包括用于机器人、环境预测、规划、控制、视频预测和交互式模拟的世界模型。

### 二级重点：相关模型与方法

当工作与上述具身方向存在明确联系时，重点关注以下技术：

- 大语言模型（LLM）、视觉语言模型（VLM）以及多模态基础模型；
- 模型训练、预训练、后训练、指令微调、参数高效微调和数据配方；
- 强化学习、离线强化学习、模仿学习、强化学习与人类反馈、偏好优化和奖励建模；
- 视觉表征、视频理解、视频生成、3D/4D 感知、空间推理和时空建模；
- 长上下文、记忆、工具使用、智能体、规划、检索增强和多步推理；
- sim-to-real、领域泛化、跨 embodiment 泛化、数据效率、安全性、可靠性和可解释评测。

纯 LLM/VLM 论文可以纳入 Paper track，但优先级低于与机器人、具身任务、世界模型或实际部署有直接连接的工作。纯通用语言建模或与研究重点缺乏可验证关联的工作，不能仅因使用 LLM/VLM 获得高相关性。

## Crawler 召回原则

crawler 召回不是 Paper 准入，也不是关键词计数。source skill 应尽量保留足以让共享阶段识别现实对象、让 Paper track理解主要贡献的原始信息，并明确可见证据与缺失信息。

- 每条 source record 至少保留稳定来源 ID、标题、作者或发布方、原始 URL、发布时间、抓取时间、冻结窗口、可读正文或摘要、附件、抓取状态和异常。
- `partial` source 的有效记录必须进入共享 inventory/canonicalization；`partial` 既不等于 reject，也不等于完整成功。
- crawler 可以保存分类、关键词、机构、趋势榜单和来源热度等基础信号，但不能写死生产 Category、主观最终分数或入选结果。
- 同一论文的 arXiv、Hugging Face 和媒体记录都应保留来源身份，交由共享 canonicalization 无损归并；crawler 不以标题作为唯一身份，也不把重复出现当作质量投票。
- 普通营销、融资、人事、活动和无新增事实的转载不因命中研究关键词进入 Paper 生产判断；若对象本质不是论文，应保留准确来源材料供共享阶段分到其他 Category，而不是由 crawler 强行改造成 Paper。

## arXiv 检索配置

### 分类范围

当前优先查询以下 arXiv 分类，并允许通过配置继续扩展：

- `cs.RO`：Robotics；
- `cs.AI`：Artificial Intelligence；
- `cs.CV`：Computer Vision and Pattern Recognition；
- `cs.LG`：Machine Learning；
- `cs.CL`：Computation and Language，主要用于 LLM/VLM、智能体和多模态相关工作；
- `cs.MA`：Multiagent Systems，在多机器人、协同智能和多智能体方向有价值；
- `eess.SY`：Systems and Control，在机器人控制、自主系统和无人机方向有价值。

分类只是召回范围，不代表论文一定相关，也不决定机器 `Category`。生产日报新增以官方 announcement RSS/Atom 和根目录持久 arXiv state 为准，不以 API `submittedDate` 代替公告语义；若 source 使用 API 补充元数据，必须保留该边界。

### 关键词组

检索实现应将关键词组织为可维护主题组，而不是拼成一个巨大的单一查询：

- `embodied_robotics`：`embodied intelligence`, `embodied AI`, `robot learning`, `robot foundation model`, `robot manipulation`, `robot control`, `robot policy`；
- `arm_vla`：`vision-language-action`, `VLA`, `language-conditioned manipulation`, `vision-language policy`, `robot manipulation`, `manipulation foundation model`；
- `drone_vla_vln`：`drone`, `UAV`, `aerial robot`, `unmanned aerial vehicle`, `vision-language navigation`, `VLN`, `aerial navigation`, `autonomous flight`, `space robotics`, `orbital robotics`, `on-orbit`, `in-orbit`, `on-orbit servicing`, `space manipulation`, `satellite servicing`, `lunar robot`, `Mars robot`, `aerospace embodied AI`；
- `embodied_infra`：`robot data`, `robot dataset`, `robot simulator`, `robot simulation`, `teleoperation`, `robot deployment`, `robot benchmark`, `embodied infrastructure`；
- `world_model`：`world model`, `world models`, `video world model`, `world model for robotics`, `predictive model`, `environment model`, `interactive simulation`；
- `llm_vlm_methods`：`large language model`, `LLM`, `vision-language model`, `VLM`, `multimodal`, `agent`, `reinforcement learning`, `imitation learning`, `offline reinforcement learning`, `reward model`。

### 中文资讯别名

以下别名只用于中文来源召回或映射到既有主题组，不参与 arXiv API 查询，也不决定 Category：

- `embodied_robotics`：`具身智能`, `机器人学习`, `机器人基础模型`, `机器人操作`, `机器人控制`, `机器人策略`；
- `arm_vla`：`视觉-语言-动作`, `视觉语言动作`, `语言条件操作`, `视觉语言策略`, `机械臂`, `机器人操作`；
- `drone_vla_vln`：`无人机`, `空中机器人`, `视觉语言导航`, `空中导航`, `自主飞行`, `空天具身智能`, `航天具身智能`, `航空具身智能`, `太空机器人`, `空间机器人`, `在轨机器人`, `在轨操作`, `在轨抓取`, `在轨服务`, `卫星服务`, `太空抓取`, `月球机器人`, `火星机器人`, `低空经济`, `低空飞行器`；
- `embodied_infra`：`机器人数据`, `机器人数据集`, `机器人仿真器`, `机器人仿真`, `遥操作`, `机器人部署`, `机器人评测`, `具身基础设施`；
- `world_model`：`世界模型`, `视频世界模型`, `预测模型`, `环境模型`, `交互式仿真`；
- `llm_vlm_methods`：`大语言模型`, `视觉语言模型`, `多模态`, `智能体`, `强化学习`, `模仿学习`, `离线强化学习`, `奖励模型`。

短缩写如 `VLA`、`VLN`、`LLM`、`VLM` 需要与分类或其他语义词结合，避免单独搜索造成歧义召回。

### 时间窗口

- 日报 prepare 固定抓取 `DisplayDate` 前一个 `Asia/Shanghai` 完整自然日，使用半开区间 `[since, until)`。例如展示日期为 `2026-08-02` 时，窗口是 `[2026-08-01T00:00:00+08:00, 2026-08-02T00:00:00+08:00)`。
- 生产窗口只由 `DisplayDate` 推导，并在创建周期上下文时冻结；不使用滚动 72 小时，不从 session 启动时刻或上一次成功 publish 时间推断。相同 CycleID 的重试必须复用原窗口。
- 独立 crawler 测试可以显式传入其他带时区的 `[since, until)` 小窗口，但不能把测试默认值带回生产 prepare。所有来源在同一 prepare 中接收完全相同的边界。

## 机构与作者关注信号

机构信号只在内容准入和主要技术判断之后作为弱信号，不是论文质量保证。机构判断应依据论文 affiliation、论文页面或可验证元数据，不能仅凭作者姓名猜测。

- 国际大型科技公司和研究机构：Google/DeepMind、Microsoft、Meta、NVIDIA、Amazon、OpenAI、Apple、Tesla 等；
- 国内大型科技公司：字节跳动、ByteDance、火山引擎、阿里巴巴、Alibaba、腾讯、百度、小米、美团、华为等；
- 具身智能及机器人公司：智元机器人、Agibot、宇树科技、Unitree、优必选、UBTECH、Pi、Gen 等；
- 研究机构和实验室：上海人工智能实验室、清华大学、北京大学、浙江大学、上海交通大学、中国科学院及其相关研究所等；
- 世界一流大学和机器人研究团队，以及在具身智能、机器人学习、VLA/VLN、世界模型领域持续贡献的机构。

该列表是种子列表，不是封闭名单。新增同等级关注机构必须在评分依据中可解释，不能静默改变规则。知名机构不能改变 reject，不能抵消方向不相关或证据缺陷，也不能作为同分时唯一依据。

## Paper 准入与 triage

Paper track 只能处理 routing manifest 中冻结为 `Paper` 的对象。每个对象先做语义 triage，再对 `admit` 对象评分；`reject` 不通过加分复活。

### Admit

- **核心方向**：主要问题或贡献直接属于具身智能、机器人学习与控制、机械臂 VLA、无人机 VLA/VLN、具身数据与仿真基础设施，或服务机器人预测、规划、控制和交互的世界模型。
- **明确使能方向**：主要贡献是 VLM、多模态、视频/空间建模、强化学习、智能体、记忆、规划、数据或训练方法，且论文材料能说明对核心方向的具体使能关系、可迁移能力或关键技术价值。
- **高价值基础方法**：与核心方向连接较弱，但在基础模型、训练范式、智能体系统、重要评测或部署基础设施上有显著、可验证的新意，值得作为研究路线观察项。该层门槛高于核心方向，不能仅凭知名机构或热度准入。

### Reject

- 只在标题、分类或摘要偶然命中关键词，实际问题和贡献与研究共识无实质关系；
- 常规应用包装、小幅工程调整或差异不清，无法说明研究价值或技术迁移价值；
- 仅汇总或转述其他工作，没有独立研究贡献；
- 合并全部允许证据后仍无法建立实际问题、主要贡献或最低可信依据；
- 已由共享阶段确认与上一周期机器候选为同一现实对象。续作、独立公告和不能高置信确认的疑似对象应保留，不得误排除。

跨周期 snapshot 只用于共享身份排除。上一周期 `Score`、`Summary`、`Selected`、`Category` 或 `PublishContent` 均不得影响本期 Paper 准入和评分。

## Paper 证据与定向补全

评分与比较必须区分“来源材料声称”和“track 已从全文核验”。triage 可依据 canonical source 材料；refine 必须获取并通读公开全文，优先级为作者或论文官方材料。

### 来源权威顺序

1. 论文原文、补充材料、作者公开 TeX、官方项目页及其代码/数据仓库；
2. arXiv/会议元数据和作者、机构的直接说明；
3. 独立技术报道或可信二手分析，只用于补充背景和外部反应；
4. 聚合页、榜单、转载和社交讨论，只作为发现与热度线索。

低层来源不能推翻高层原文，不能把未核验主张写成事实。来源数量、Hugging Face 排名和多源出现不是方法质量或实验可信度投票。

### Triage enrichment

只有同时满足以下条件时，Paper triage 才可按 canonical record 中的原始 URL 定向补全：合并来源后仍只有标题或通常不超过一两句实质描述；当前证据不足以判断主要贡献或评分依据；补全可能改变准入或同 track cutoff。`partial`、低置信度或接近 cutoff 本身不能触发补抓，不为明显无关或已有充分 abstract 的对象批量补抓。

无法安全获取或补全后仍证据不足时，依据可见证据保守判断；若连实际技术贡献和共识连接都无法建立，以 `insufficient_evidence_after_enrichment` 拒绝。任何阶段都不得补写材料中不存在的实验、机构、结论或影响。

## Paper 评分量表

Paper track 使用独立 100 分制，保存分项分数、证据、缺口和评分置信度。该总分仅用于 Paper track 内校准、比较和 cutoff；它写入 Paper 的 `group_score`，与 News、Policy score domain 均不可比较。

| 维度 | 权重 | 参考问题 |
| --- | ---: | --- |
| 方向相关性 | 30 | 是否直接服务核心方向，或对核心方向具有具体、重要的使能关系？ |
| 方法与问题新颖性 | 20 | 是否提出新模型、训练范式、数据/系统设计或重要问题定义？ |
| 技术与实验可信度 | 20 | 是否有合理实验、基线、消融、真实机器人或其他可复查证据？ |
| 潜在影响与可迁移性 | 15 | 是否可能影响研究路线、工程系统或跨任务/跨 embodiment 泛化？ |
| 机构与作者信号 | 10 | 可验证团队是否在相关方向持续贡献？ |
| 时效性与组内讨论价值 | 5 | 是否需要团队近期跟进并可能改变研究或评测判断？ |

### 评分解释

- 方向相关性：`27-30` 为主要贡献直接属于核心方向；`22-26` 为明确且重要的使能；`15-21` 为具体但次要的迁移连接，或门槛很高的高价值基础方法；`0-14` 通常不满足 Paper 生产准入，不能依靠其他维度补分。
- 方法与问题新颖性：`17-20` 为明显新范式、关键能力或重要问题定义；`13-16` 为清晰实质创新；`8-12` 为有价值但偏增量；`0-7` 为常规组合、差异不清或新包装。
- 技术与实验可信度：`17-20` 为充分、多角度且可复查的验证；`13-16` 为较完整验证但仍有关键细节待全文确认；`8-12` 为初步或单一类型证据；`0-7` 为证据很少或主张难以判断。摘要中的实验必须表述为“论文报告”，不能假装已核验全文。
- 潜在影响与可迁移性：`13-15` 为可能直接影响核心研究路线、关键系统或跨任务能力；`9-12` 为影响路径明确且适用较广；`5-8` 为局部迁移或讨论价值；`0-4` 为路径空泛或材料不支持。
- 机构与作者信号：`8-10` 仅用于可验证且团队持续重要贡献的情况；`4-7` 为可信且方向相关团队；`1-3` 为弱背景信号；`0` 为未知或无法验证。该维度不能改变 reject 或抵消证据缺陷。
- 时效性与组内讨论价值：`4-5` 表示需要近期调整研究、评测或部署判断；`2-3` 表示有明确跟进价值；`0-1` 表示只因刚发布、热门或被多源收录。多源关注只能作为本维度内弱背景，不另设跨源 bonus。

真实机器人实验、开放数据/代码、失败分析和跨环境验证可作为可信度与影响力的正向证据，但缺少任一项不自动判定论文无价值。所有分数都必须落到可定位的当前材料证据，缺失信息保守计分并明确记录。

评分置信度与分数分开保存：`high` 表示材料足以支持主要判断；`medium` 表示方向和贡献明确但关键实验或细节只来自摘要；`low` 表示来源过短、主张难以核实或重要信息缺失。低置信度不自动淘汰高潜力论文，但 cutoff 复核必须重开证据。

## Paper 内比较、cutoff 与容量

Paper track 只能在 Paper 候选之间比较。总分用于校准，不机械决定顺序：`90-100` 应极少出现，代表本期异常重要且证据充分；`80-89` 为强候选；`70-79` 为明确有价值；`60-69` 为需要 Paper 内比较的边界项；低于 `60` 通常不入选。相近分数可依据贡献独立性、证据置信度、主题重复和本期研究价值调整 Paper 内相对顺序，但必须记录理由。

- 三个 track 完全隔离。Paper 固定接收 `paper_capacity=10`，News primary target 为 5 且最多输出 10 条有序合格储备，Policy target/最大值为 3；协调器只允许 Policy 数量不足时按 News 自身顺序补足 News+Policy=10，不得让 Paper 补 News/Policy 数量缺口，也不得跨轨比较内容或分数。
- `selection_limit` 在 production schema v3 中固定冻结为 `20`。Paper 生产目标和最低数量都是 10 条。只要去重后存在至少 10 条通过研究方向准入、身份明确且有摘要或论文材料支持主要贡献判断的 Paper，就必须选满至少 10 条。
- 若严格 cutoff 只留下不足 10 条，必须从已通过方向准入的边界工作池中按相关性、贡献独立性和本期价值依次补足到 10；摘要可用于确认论文报告的贡献和实验，不要求 rating 阶段已经完成全文核验。明显无关、只有关键词偶然命中、身份不明或无法建立最低可信依据的对象仍不得作为 filler。只有当本期相关 Paper 实际不足 10 条时才允许少选，并在 cutoff artifact 写明不足原因和实际可用数量。
- selected Paper 的 `group_rank` 按 Paper track 冻结顺序在 `Paper` 组内从 1 连续编号，`group_score` 保存 Paper 总分。
- `Paper` 组 `group_rank` 为 `1-3` 的条目获得 presentation emphasis。emphasis 只是展示属性，不改变评分、顺序或入选结果。

## Paper 全文 refine

Paper refine 在 Paper track 的独立 clean context 中处理冻结 selected 集合，不增删、替换、重新评分或重新分类。必须真正获取并通读公开全文，优先使用已有或下载的 arXiv TeX 源码，其次是 PDF、论文 HTML、官方项目页和补充材料；只改写 crawler 摘要不算完成。

每篇 Paper refined article 至少包含：

1. 标题与一句话结论；
2. 研究问题与背景，说明它与核心方向的具体连接；
3. 方法与系统，解释关键设计而不是只列术语；
4. 数据、实验设置、基线和评测条件；
5. 关键结果，明确区分论文报告值、作者主张和 track 分析；
6. 局限、失败案例、证据缺口与尚未验证的外推；
7. 对组内研究、工程或评测的启示；
8. 原始论文、项目、代码、数据和必要补充来源链接。

文章必须能脱离临时文件独立发布，不能包含本地路径、占位符或未解释分数。可选预览图必须来自允许使用且可追溯的论文图或项目素材，并记录来源；无预览图可降级，不得生成伪造占位图。有限重试后仍缺完整 refined Markdown 的单篇候选必须以明确原因 `dropped`，不能伪装为成功，也不阻断其他候选完成。

Paper track 必须为 selected 集合生成 immutable refine manifest，逐条记录 `success | dropped`，并以成功集和 dropped 集穷尽且互斥覆盖输入；空 selected 也必须生成可验证的完成态空 manifest。它不读取 News 或 Policy refine 产物，也不执行跨 track taxonomy。只有协调器确认 Paper、News、Policy 三路 refine 都完成输入对账后，才由 `refine-candidates` 在规范 `runs/<REFINE_RUN_ID>/assemblies/<ASSEMBLY_GENERATION_ID>/` 路径独占对成功项执行 taxonomy 与 grouped publication assembly；唯一 pointer 是 `working_tmp/refine_candidates/current.json`，不得创建 rating-owned 或 post_refine alternative。

## 输出与反馈

- Paper track 输出自己的 triage、评分、Paper 内比较、cutoff、selected 顺序、`group_score` 和 refine manifest。
- Paper refine 完成后把 immutable manifest 交还协调器；Paper track 不自行执行共享 taxonomy 或 publication assembly。
- 内容 hash 不是业务字段或完成条件；不得计算、保存或比较 publication hash、manifest hash 或字段 hash。幂等和归属沿用 `CycleID`、`Publish`、`CandidateID`、row ID、受管路径、明确字段值和 sink 回读。

每轮人工筛选后，优先记录漏掉的论文、无关结果、机构信号偏差、Paper 评分维度偏差、版本与跨来源身份错误，以及全文 refine 的事实或结构问题。每次修改应保持规则清晰、可解释，避免为单篇论文添加过拟合例外。
