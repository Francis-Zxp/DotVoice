/* 重点内容 — offline, deterministic emphasis for dictated text (no model, no network).

   Every mark carries a two-character label so it fits above a word in VoiceScore:
     约束 要求 结论 待定 推测 待办 期限 时间 时长 问题 设想
     金额 比例 折扣 数字 数量 数据 版本 命令 文件 链接 邮箱 代码 术语 标题 命名 引用
     人物 机构 地点 要点
   1. Structured rules find claims and facts a reader acts on: constraints and
      requirements keep their negation/modal ("不要自动发送"), action items keep
      their owner and deadline ("月底前要把反馈整理出来"), decisions, open points,
      questions, Chinese times/dates/ranges/relative deadlines, money, ratios,
      quantities with units, versions, commands, paths, quoted names, people
      with a title, organisations and places.
   2. A small TextRank over Intl.Segmenter words (with a Chinese stopword list)
      offers a 要点 only for specific, non-generic phrases; it never outranks (1).
   3. Selection is greedy by score with a per-clause penalty (marks spread over
      the utterance), a count cap, an "ink" budget (CJK 1, ASCII ~½) and
      de-duplication, so long dictations stay calm and fillers are never marked.
   Nothing here rewrites the transcript or claims ASR word timing. */

export interface Mark { start: number; end: number; label: string }

interface Cand extends Mark { score: number; atom: boolean; lead: number }
interface Clause { start: number; end: number; sep: string }
interface Seg { segment: string; index: number; isWordLike?: boolean }

const list = (s: string) => new Set(s.trim().split(/\s+/));

/* ── lexicon ──────────────────────────────────────────────────────────── */

// A whole mark equal to one of these is never shown.
const FILLER = list(`嗯 呃 额 啊 哦 唉 就是 就是说 然后 然后呢 那个 这个 其实 反正 我觉得 我认为 我想 实际上 基本上
  一般来说 总而言之 总的来说 说实话 对吧 是吧 好吧 好的 可以 可以的 没问题 这个时候 的情况下 怎么说呢 你知道吗`);

// Function words, discourse fillers, pronouns and light verbs: never part of a 要点.
const STOP = list(`嗯 呃 额 啊 哦 噢 喔 唉 哎 诶 呀 吧 呢 嘛 啦 咯 哈 哟 么 吗 那个 这个 那些 这些 那种 这种 那样 这样 这么 那么
  怎么 什么 为什么 怎样 如何 就是 就是说 然后 然后呢 其实 反正 总之 所以 所以说 因为 但是 可是 不过 而且 并且 或者 还是
  如果 的话 要是 虽然 即使 只要 只有 除了 于是 因此 由于 否则 不然 我 你 他 她 它 我们 你们 他们 她们 它们 咱们 大家
  自己 别人 人家 您 我觉得 我认为 我想 觉得 认为 感觉 好像 似乎 应该 可能 也许 大概 估计 一下 一点 一些 有点 有些 一个
  一种 一样 一直 一定 一起 一般 一切 一共 已经 正在 还有 另外 比如 例如 包括 关于 对于 根据 通过 按照 以及 也就是说
  换句话说 实际上 基本上 一般来说 总而言之 总的来说 说实话 老实说 对吧 是吧 好吧 好的 行吧 可以 可以的 没问题 没事 没有
  不是 只是 都是 也是 这是 那是 就行 这里 那里 这边 那边 哪里 这儿 那儿 现在 当时 时候 这时 那时 今天 刚才 刚刚 之前
  之后 以后 以前 后来 最后 首先 其次 接着 再说 主要 主要是 东西 事情 意思 样子 各种 有的 所有 每个 很多 许多 不少 非常
  特别 比较 相当 稍微 真的 确实 当然 其他 别的 等等 之类 什么的 一会儿 待会儿 顺便 进行 使用 需要 开始 继续 出现 发现
  表示 提出 存在 导致 造成 产生 获得 得到 成为 作为 属于 知道 告诉 希望 打算 准备 做成 变成 还要 不要 不能 必须 记得
  之间 中间 里面 上面 下面 之中 当中 期间 方面 以上 以下 之内 以内 左右 一次 再按 长按 按住
  影响 采用 决定 确定 解释 用来 用于 挺好 还挺 不错 怎么样 看看 想到 说说 讨论 出来 起来 下去 回来 过来 过去`);

// Single characters that cannot join a key phrase.
const FUNC = new Set([...'的了在是和与及或把被给对向从就都也还又很更最太再才而但并等着过地得吗呢吧啊么个这那哪我你他她它们一不没有会能要去来到说想看让用做将于以为之其此每各某些吃拿放挂叫找时里后前中上下内外']);

// Common nouns that are too general to be a 要点 on their own.
const GENERIC = list(`问题 情况 方案 东西 事情 方面 部分 内容 方式 方法 过程 结果 原因 背景 时间 地方 工作 项目 数据 系统
  用户 功能 效果 目标 计划 结构 方向 水平 能力 作用 关系 条件 环境 状态 模式 版本 信息 资料 文件 材料 天气 时候 东西`);

const NOUN_TAIL = /^[性度率组表室器法论学化感力量值型式体者员家品类物群区栏键稿单]$/u;
const VERB_TAIL = list('测量 分开 认定 进行 提高 降低 增加 减少 解释 上线 讨论 循环 出字 变成 招募 成立 生效 见面 出来 起来');

const EN_FILLER = list(`a an the and or but i we you he she they it is are was were be been to of in on at by for with from this
  that these those please okay ok yes no not just so can could would should will do does did have has had my your our their
  me us them its as if then than also very really about what which who how when where why there here some any all more most
  much many get got make let thanks thank hello hi hey sure well today now`);

// Names that count as technical terms even when written in lower case.
const TECH = list(`python rust react vue svelte angular nuxt docker kubernetes k8s redis mysql postgres postgresql sqlite
  mongodb nginx linux windows macos ios android git github gitlab node deno bun java kotlin swift golang typescript
  javascript tauri electron flutter figma notion slack excel word powerpoint ppt zotero latex markdown json yaml html css
  sql http https wifi bluetooth usb esp32 openai claude chatgpt gpt llm whisper sherpa onnx cuda pnpm npm yarn vitest vite
  webpack playwright numpy pandas pytorch torch tensorflow matlab spss jupyter vscode chrome edge safari firefox wechat`);

const EN_TIME = list('monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october november december');

/* ── numbers ──────────────────────────────────────────────────────────── */

const CN = '零〇一二两三四五六七八九十百千万亿';
const NUM = `(?:\\d+(?:,\\d{3})*(?:\\.\\d+)?(?:\\s?[万亿])?|[${CN}]+)`;
// A number starts here (not inside another number, a weekday or an ordinal).
const NS = `(?<![${CN}\\d.,第周期拜初])`;
const SUFFIX_LIMIT = '(?:以内|以上|以下|左右|上下|出头)';

/** A well-formed number: Arabic, or Chinese without "千万"/"一一"-style accidents. */
function wellFormed(n: string) {
  n = n.replace(/[\s,]/g, '');
  if (/^\d/.test(n)) return /^\d+(?:\.\d+)?[万亿]?$/.test(n);
  return /[一二两三四五六七八九十]/.test(n) && !/^[万亿百千零〇]/.test(n) && !/[一二两三四五六七八九]{2}/.test(n);
}
/** A number with real weight: not a lone "一/两/五" (一块, 两个) and not 0 or 1. */
function weighty(n: string) {
  n = n.replace(/[\s,]/g, '');
  return wellFormed(n) && (/^\d/.test(n) ? !/^[01]$/.test(n) : [...n].length >= 2);
}

const MONEY_UNIT = '(?:万元|亿元|美元|美金|欧元|日元|英镑|港币|港元|人民币|块钱|块|毛钱|元(?![化素论次组件旦首宵]))';
const MONEY_CTX = '销售额|营收|营业额|收入|利润|成本|费用|预算|价格|售价|单价|总价|报价|工资|薪资|薪水|奖金|房租|租金|花了|花费|价值|估值|融资|市值|开销|报销|学费|运费|定金|押金|首付|月薪|年薪|价钱|要价';
const DUR_UNIT = '(?:毫秒|秒钟|秒|分钟|小时|钟头|天|周(?!年)|星期|礼拜|个月|工作日|年(?![级代度轻纪龄终会]))';
const MEAS_UNIT = '(?:毫克|千克|公斤|克(?![服隆制])|斤|吨|毫升|升(?![级值高])|平方米|立方米|平米|厘米|毫米|公里|千米|米(?![饭粉线])|摄氏度|度(?![过假])|千瓦|瓦|伏|毫安时|毫安|帧|像素|字节|英寸|寸|岁|分贝|公顷|亩)';
const COUNT_UNIT = '(?:个|名|位|人(?![家民工生类])|次|条|页|篇|章|节(?![日目奏省])|行(?![业为动李])|字(?![符体母])|项|台|件|套|份|本(?![来身地])|张|首|部|场|轮|期|批|组|家|所|辆|道|题|款|种|类|处|株|只|头|颗|座|间|句)';
const ASCII_UNIT = '(?:ms|min|hrs?|kg|mg|ml|mL|cm|mm|km|mAh|mA|kWh|Wh|kW|kHz|MHz|GHz|Hz|KB|MB|GB|TB|Kbps|Mbps|fps|dpi|ppi|px|dB|°C|°F|℃|[smhgLWVKkxw])';

const MONEY_RE = new RegExp(`(?:[¥￥$€£]\\s?\\d+(?:,\\d{3})*(?:\\.\\d+)?(?:\\s?[万亿kKwW])?|${NS}(${NUM})(?:多|余)?(${MONEY_UNIT}))${SUFFIX_LIMIT}?`, 'gu');
const MONEY_CTX_RE = new RegExp(MONEY_CTX, 'gu');
const MONEY_NUM_RE = new RegExp(`${NS}(${NUM})(?:多|余)?${SUFFIX_LIMIT}?(?![${CN}\\d.%％‰个名人次条页克米斤吨岁倍折年月日号点天周元块分秒小钟])`, 'gu');
const RATIO_RE = new RegExp(`(?:百分之|千分之)[零〇一二两三四五六七八九十百点\\d.]+|(?<![\\d.])\\d+(?:\\.\\d+)?\\s?[%％‰]|${NS}${NUM}个?百分点|${NS}(${NUM})倍|${NS}[一二三四五六七八九十]成(?=[的左以多，。！？、\\s]|$)`, 'gu');
const DISCOUNT_RE = new RegExp(`打?${NS}[一二三四五六七八九]{1,2}折(?![叠腾中射线磨服扣])|(?<![\\d.])\\d(?:\\.\\d)?折(?![叠腾中射线磨服])`, 'gu');
const DUR_RE = new RegExp(`(过)?${NS}((?:${NUM})个?(?:多|半)?|半个?)(${DUR_UNIT})(半)?(以后|之后|后|以内|之内|内|之前|以前|前(?![天年面台端提期])|左右)?`, 'gu');
const MEAS_RE = new RegExp(`${NS}(${NUM})(?:多|余)?\\s?${MEAS_UNIT}`, 'gu');
const COUNT_RE = new RegExp(`${NS}(${NUM})(?:多|余)?\\s?${COUNT_UNIT}`, 'gu');
const ASCII_QTY_RE = new RegExp(`(?<![\\w.])\\d+(?:\\.\\d+)?\\s?${ASCII_UNIT}(?![A-Za-z0-9])`, 'g');
const BIG_RE = new RegExp(`${NS}(?:\\d+(?:\\.\\d+)?\\s?[万亿]|[零〇一二两三四五六七八九十百千]+[万亿]|\\d{1,3}(?:,\\d{3})+)(?![${CN}\\d元块%％倍个名人])`, 'gu');
const STAT_RE = /(?<![A-Za-z0-9_])(?:[npdrtFzβ]|df|SD|SE|R2|r2|R²|r²|η²|ω²|χ²)\s*[=<>≤≥]\s*-?\d*\.?\d+%?/g;

/* ── time ─────────────────────────────────────────────────────────────── */

const D = '[零〇一二两三四五六七八九十\\d]';
const WD = '[一二三四五六七日天末1-7]';
const DAYN = '(?:\\d{1,2}|[一二三四五六七八九十]{1,3})';
const FUT = `(?:大后天|后天|明天|明早|明晚|明儿|今晚|今早|今夜|下下(?:个)?(?:周|星期|礼拜)${WD}?|下(?:个)?(?:周|星期|礼拜)${WD}?|(?:这|本)(?:个)?(?:周|星期|礼拜)${WD}|(?:周|星期|礼拜)${WD}|下(?:个)?月(?:初|底|中|末|${DAYN}[号日])?|(?:这|本)(?:个)?月(?:初|底|中|末|${DAYN}[号日])|月(?:初|底|中|末)|(?:明|今)年(?:初|底|中|末)|年(?:初|底|中|末)|明年|下半年|下(?:个)?季度|(?:本|这)(?:个)?季度(?:末|初)|季度(?:末|初)|节(?:前|后)|春节|国庆|元旦|中秋|端午|清明|五一|寒假|暑假)`;
const PAST = `(?:昨天|昨晚|前天|上(?:个)?(?:周|星期|礼拜)${WD}?|上(?:个)?月(?:初|底|中|末)?|去年(?:初|底|中|末)?|前年|上半年|上(?:个)?季度)`;
const NOW = '(?:今天|今儿|当天|这(?:个)?(?:周|星期|礼拜)|本周|这(?:个)?月|本月|今年|本季度|这(?:个)?季度)';
const DATE = `(?:(?:(?:\\d{4}|[〇零一二三四五六七八九]{4})年)?${DAYN}月(?:${DAYN}[日号]|初|底|中|末|份)|\\d{4}[-/.]\\d{1,2}[-/.]\\d{1,2}|\\d{4}年(?:${DAYN}月)?|${DAYN}号(?=之前|以前|前|截止|上午|下午|晚上|中午|早上))`;
const RECUR = `每(?:天|日|晚|周${WD}?|星期${WD}|个?月|年|个?工作日)`;
const PART = '(?:凌晨|清晨|早上|早晨|上午|中午|午后|下午|傍晚|晚上|夜里|半夜|深夜)';
const CLOCK = `(?:${D}{1,3}点(?:钟|半|整|一刻|三刻|${D}{1,3}分?)?|\\d{1,2}[:：][0-5]\\d(?:\\s?[aApP][mM])?|\\d{1,2}\\s?[aApP][mM](?![A-Za-z]))`;
const POINT = `(?:(?:${RECUR}|${DATE}|${FUT}|${PAST}|${NOW})(?:的(?=${PART}))?(?:${PART})?(?:${CLOCK})?|${PART}(?:${CLOCK})?|${CLOCK})`;
const TIME_RE = new RegExp(`(最晚|最迟|不晚于|截止到|截止|截至|务必在|必须在|最好在|争取在)?(${POINT})(?:\\s?(?:到|至|~|～|-|—|－)\\s?(${POINT}))?(之前|以前|前(?![天年面台端提期])|为止|截止|以内|之内|内(?![容部外])|以后|之后|后(?![天年面台端果来续])|左右)?`, 'gu');
const EVENT_DEADLINE_RE = /(?:下班|上班|放学|开会|午饭|晚饭|吃饭|睡觉|周会|例会|出发|登机|月结)(?:之前|以前|前)/gu;
const EN_TIME_RE = /\b(?:(before|by|until|due|no later than)\s+)?(?:(?:next|this)\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|[Tt]omorrow|[Tt]onight|EOD|end of (?:the )?(?:day|week|month)|next (?:week|month))\b|\b\d{1,2}(?::\d{2})?\s?(?:am|pm|AM|PM)\b/g;

const at = (s: string) => new RegExp(`^(?:${s})`, 'u');
const IS_DAY = at(`${RECUR}|${DATE}|${FUT}|${PAST}|${NOW}`);
const IS_FUT = at(FUT), IS_PAST = at(PAST), IS_NOW = at(NOW), IS_RECUR = at(RECUR), IS_DATE = at(DATE);
const HAS_PART = new RegExp(PART, 'u'), HAS_CLOCK = new RegExp(CLOCK, 'u');
const VAGUE = /^(?:下下?(?:个)?(?:周|星期|礼拜)|下(?:个)?月|明年|下半年|下(?:个)?季度|(?:本|这)(?:个)?季度(?:末|初)|季度(?:末|初))$/u;
const CLOCK_CUE = /^\s?(?:开会|见面|见|集合|出发|起床|上课|下课|上班|下班|开始|结束|准时|碰头|开播|上线|发布|左右|多)/u;

/* ── people, organisations, places ────────────────────────────────────── */

const SURNAME = '王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤欧';
const SURNAME_STRONG = '王李张刘陈杨黄赵吴徐孙朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾肖董袁潘蒋蔡杜苏魏吕丁沈姚卢姜崔谭陆汪范廖贾邹孟熊秦邱薛段侯陶黎贺顾郝龚邵严戴孔';
const TITLE = '老师傅|老师|教授|博士|医生|大夫|律师|经理|总监|总裁|董事长|主任|院长|校长|部长|处长|科长|局长|书记|主席|老板|师傅|先生|女士|小姐|同学|阿姨|叔叔|总(?![是结共之体的算计部额数量价有会])|工(?![作具程资厂人业艺])';
const PERSON_RE = new RegExp(`[${SURNAME}](?:${TITLE})|[小老][${SURNAME_STRONG}]|[${SURNAME_STRONG}][\\p{Script=Han}]{1,2}(?=来?负责|牵头|主持|跟进|主讲|汇报|发言)`, 'gu');
const ORG_RE = /大学|学院|研究所|研究院|研究中心|医院|公司|集团|银行|法院|检察院|委员会|协会|基金会|事务所|出版社|电视台|中学|小学|幼儿园/gu;
const PLACE_RE = /会议室|办公室|教室|实验室|报告厅|礼堂|大厅|食堂|餐厅|咖啡馆|咖啡厅|酒店|宾馆|机场|火车站|高铁站|地铁站|车站|图书馆|体育馆|广场|公园|大楼|大厦|校区|园区|商场/gu;
const CITY_RE = /北京|上海|广州|深圳|杭州|南京|苏州|成都|武汉|西安|重庆|天津|厦门|青岛|长沙|郑州|合肥|济南|大连|沈阳|哈尔滨|昆明|福州|宁波|无锡|东莞|佛山|珠海|香港|澳门|台北|新加坡|东京|大阪|首尔|纽约|伦敦|巴黎|柏林|旧金山|硅谷|洛杉矶|西雅图|浦东|浦西|海淀|中关村|陆家嘴|虹桥/gu;

/* ── clause-level cues ────────────────────────────────────────────────── */

const ACTS = '创建|新建|实现|修复|修改|修正|改成|改为|优化|调整|重构|替换|合并|拆分|迁移|升级|回滚|安装|卸载|下载|上传|导出|导入|提交|推送|发布|上线|部署|配置|运行|重启|关闭|打开|启用|禁用|新增|添加|增加|删除|删掉|去掉|移除|补充|完善|编写|生成|设计|计算|统计|分析|对比|比较|汇总|整理|总结|翻译|润色|校对|核对|检查|排查|测试|验证|确认|联系|通知|预约|预订|购买|准备|安排|收拾|打电话|发邮件|发消息|回复|跟进|评估|审核|审阅|复查|标注|备份|清理|定位|保存|保留|读取|记录|显示|隐藏|更新|发送|提醒|处理|解决|完成|交给|发给|转给|告诉|问问|打印|报名|申请|填写|签字|缴费|提前|预留|落实|汇报|讨论一下|商量一下|开个会';
const ONE_ACT = '[改加删查修写订买交发问打跑看做试测换调装填寄带算搜找画录拍传存印](?=一下|一遍|个|一个|掉|好|完)';
const DOABLE = `(?:把|将(?![近来要会])|给|跟|和|去|先|再|尽快|马上|立刻|及时|按时|${ACTS}|${ONE_ACT})`;
const IS_ACT = new RegExp(`^(?:${ACTS})$`, 'u');
const IMPERATIVE = new RegExp(`^(?:把|将(?![近来要会])|${ACTS}|${ONE_ACT})`, 'u');
const SEQ = /^(?:\s|先|再|然后|接着|最后|顺便|另外|还有|同时|也|并且|并|还要|马上|立刻|赶紧|尽快|大家|你们|你|您)+/u;
// Conjunctions, time clauses and spoken fillers end a phrase (unpunctuated preview text needs the fillers).
const BREAK = /然后|但是|(?<!不)但|不过|可是|并且|而且|同时|以及|还有|另外|接着|之后|以后|的时候|再看|再说|再议|因为|所以|如果|要是|的话|以便|免得|否则|不然|顺便|嗯|呃|就是说|我觉得|我认为|我想说/u;
const TRAIL = /(?:\s|[啊吧嘛呀哦哈啦咯哟噢喔]|就行了?|就好了?|就可以了?|即可)+$/u;
const STATEMENT = /很|太|非常|挺|比较|特别|已经|曾经|是|了(?!解)|过(?![一来去])|不错/u;

// keep: the cue word stays in the mark (negation/modal changes the meaning); min: shortest phrase (ink).
interface Cue { re: RegExp; label: string; score: number; keep: boolean; min?: number }
const CUES: Cue[] = [
  // Negative constraints keep their negation; "要不要/能不能" are questions, "不得不" is a requirement.
  { re: /(?<![要能得可])(?:千万|绝对)?(?:不要|不能|不可以|不得|不许|不准|不允许|不应该?|禁止|严禁|切勿|避免)(?!紧|不|已|忘|了吗)/gu, label: '约束', score: 112, keep: true },
  { re: /^(?:\s*(?:你们?|您|大家|咱们|我们|千万|先|也|都|就|可))*(?:别|不用|不必|无需|甭)(?![人的处样扭墅致名称字具提说忘])/gu, label: '约束', score: 104, keep: true },
  { re: /(?<!不)(?:必须|务必|一定要|一定得|需要确保|要确保|确保|要保证|必须保证|不得不|不能不|切记|千万要)/gu, label: '要求', score: 110, keep: true },
  { re: /注意(?![力到的事])|最好(?![的是])|尽量|尽可能/gu, label: '要求', score: 94, keep: true },
  { re: /(?:可能|也许|或许|大概率|估计|疑似|推测|猜测|怀疑)(?!性|的话)/gu, label: '推测', score: 100, keep: true },
  { re: /尚未|仍未|并未|未能|待定|待确认|待验证|有待|不确定|不清楚|说不准|不一定|仍需|需要进一步|还没有?(?=确定|决定|定|验证|确认|完成|做完|搞定|解决|处理|开始|上线|回复|收到|测试|想好)|还需要?(?=进一步|验证|确认|讨论|研究|核实|评估|测试|观察|商量|实验|检验|重复)/gu, label: '待定', score: 100, keep: true },
  { re: /(?<![不没未难])(?:最终|最后)?(?:决定了?|确定了?|敲定了?|定下来了?|拍板|选定|商定|一致同意|达成一致|同意了)(?![吗吧呢么]|不)/gu, label: '结论', score: 104, keep: true },
  { re: /结论(?:就)?是|结论为|最终结论是?|答案(?:就)?是/gu, label: '结论', score: 104, keep: false, min: 3 },
  { re: /记得要?|记住要?|别忘了|别忘记|不要忘了|不要忘记|千万别忘了?|提醒我|提醒一下|回头(?![看想])|改天|抽空/gu, label: '待办', score: 96, keep: false },
  { re: new RegExp(`(?<![不想只主重需将快就要])(?:需要|要)(?=\\s?${DOABLE})`, 'gu'), label: '待办', score: 94, keep: false, min: 3 },
  { re: new RegExp(`(?<![邀申很太好真挺有])(?:请(?![假教问客])|麻烦(?:你|您)?|帮我|帮忙|劳驾|拜托(?:你|您)?)(?=\\s?${DOABLE})`, 'gu'), label: '待办', score: 90, keep: false, min: 3 },
  { re: /(?:负责|牵头|跟进|落实|安排)(?![人的了])/gu, label: '待办', score: 96, keep: true },
  { re: /如果能|要是能|如果可以|要是可以|可以考虑|不如|或许可以|也许可以|有个想法是?|有个点子是?|我的想法是|突发奇想|想到一个/gu, label: '设想', score: 74, keep: false },
];
const HEADER = /(结论|决定|决议|总结|待办|TODO|todo|行动项|下一步|问题|注意事项|要求)\s*$/u;
const HEADER_LABEL: Record<string, string> = { 结论: '结论', 决定: '结论', 决议: '结论', 总结: '结论', 待办: '待办', TODO: '待办', todo: '待办', 行动项: '待办', 下一步: '待办', 问题: '问题', 注意事项: '要求', 要求: '要求' };
const QWORD = /为什么|为啥|怎么样|怎么办|怎么|如何|是否|是不是|能不能|可不可以|会不会|要不要|有没有|对不对|行不行|好不好|多少|几点|几个|几天|哪里|哪儿|哪个|哪些|什么时候|什么|谁/u;
const ANOTA = /是不是|能不能|可不可以|会不会|要不要|有没有/u;
const QFILLER = /^(?:对吧|是吧|好吧|对不对|是不是|好不好|你知道吗|知道吗|懂吗|怎么说呢|你说呢|对吗|行吗|好吗|可以吗)$/u;
const LEAD_FILLER = /^(?:\s|嗯|呃|啊|那个|就是说?|然后|所以|那么?|其实|我想问一下|问一下|请问|我想知道|我们|咱们)+/u;

/* ── helpers ──────────────────────────────────────────────────────────── */

/** Visual weight: CJK 1, ASCII about half, whitespace nothing (a surrogate pair counts once). */
function ink(s: string) {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 32 || c === 9 || c === 10 || c === 13 || c === 0xa0 || c === 0x3000 || (c >= 0xdc00 && c <= 0xdfff)) continue;
    n += c < 128 ? .55 : 1;
  }
  return n;
}

let segmenter: Intl.Segmenter | null | undefined;
function segment(text: string): Seg[] {
  if (segmenter === undefined) {
    try { segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('zh', { granularity: 'word' }) : null; }
    catch { segmenter = null; }
  }
  return segmenter ? [...segmenter.segment(text)] : [];
}

function splitClauses(text: string): Clause[] {
  const out: Clause[] = [];
  let start = 0;
  for (const m of text.matchAll(/[，。！？；：\n!?;…]+|,(?!\d)|[.:](?=\s|$)/g)) {
    if (m.index! > start) out.push({ start, end: m.index!, sep: m[0] });
    start = m.index! + m[0].length;
  }
  if (start < text.length) out.push({ start, end: text.length, sep: '' });
  return out;
}

/* ── analysis ─────────────────────────────────────────────────────────── */

interface Analysis { cands: Cand[]; clauses: Clause[]; clauseAt: Int32Array }

// Notes re-render often and VoiceScore re-measures; the analysis of a text never changes.
const memo = new Map<string, Analysis>();
function analysis(text: string) {
  let hit = memo.get(text);
  if (hit) { memo.delete(text); memo.set(text, hit); return hit; }
  hit = analyze(text);
  memo.set(text, hit);
  if (memo.size > 24) memo.delete(memo.keys().next().value!);
  return hit;
}

function analyze(text: string): Analysis {
  const clauses = splitClauses(text);
  const clauseAt = new Int32Array(text.length + 1).fill(-1);
  clauses.forEach((c, i) => clauseAt.fill(i, c.start, c.end));
  const segs = segment(text);
  const bounds = new Set<number>([0, text.length]);
  for (const s of segs) { bounds.add(s.index); bounds.add(s.index + s.segment.length); }
  const boundary = (i: number) => !segs.length || bounds.has(i);
  const han = /\p{Script=Han}/u.test(text);
  const cands: Cand[] = [];

  const add = (start: number, end: number, label: string, score: number, atom = true, lead = start) => {
    while (start < end && /\s/.test(text[start])) start++;
    while (end > start && /\s/.test(text[end - 1])) end--;
    const value = text.slice(start, end);
    const weight = ink(value);
    if (weight < 1.1 || weight > 24 || FILLER.has(value) || /^[\p{P}\s]*$|^\d+$/u.test(value)) return;
    // Long claims read worse in a bracket; a long path or URL is still one fact.
    cands.push({ start, end, label, score: atom ? score : score - Math.max(0, weight - 12) * 1.5, atom, lead: Math.min(lead, start) });
  };
  const each = (re: RegExp, fn: (m: RegExpExecArray) => void) => { for (const m of text.matchAll(re)) fn(m as RegExpExecArray); };

  /** The phrase from `from` to the end of its clause, cut at a conjunction (or a soft
      boundary in unpunctuated text) and trimmed. */
  let soft: number[] = [];
  const phrase = (from: number, end: number, min: number, max: number, trail = TRAIL): [number, number] | null => {
    while (from < end && /\s/.test(text[from])) from++;
    const stop = soft.find((p) => p > from && ink(text.slice(from, p)) >= 4);
    let s = text.slice(from, stop !== undefined && stop < end ? stop : end);
    const cut = s.slice(1).search(BREAK);
    if (cut >= 0) {
      if (/^(?:之后|以后|的时候)/.test(s.slice(cut + 1))) return null; // a time clause, not an instruction
      s = s.slice(0, cut + 1);
    }
    s = s.replace(trail, '');
    const weight = ink(s);
    if (weight < min || weight > max || !/\p{Script=Han}.*\p{Script=Han}|[A-Za-z]{2}/u.test(s)) return null;
    return [from, from + s.length];
  };

  /* quoted names, titles, code */
  each(/《([^《》\n]{1,30})》|“([^“”\n]{2,24})”|"([^"\n]{2,24})"|「([^「」\n]{1,24})」|『([^『』\n]{1,24})』|`([^`\n]{1,40})`/g, (m) => {
    const inner = m.slice(1).find((g) => g !== undefined)!;
    const start = m.index + 1;
    const open = m[0][0];
    const named = /(?:叫做?|命名为|名为|称为|称作|取名为?|改名为?|起名为?|名字(?:是|叫)|标题(?:是|为)|主题(?:是|为)|代号)\s*$/u.test(text.slice(Math.max(0, m.index - 6), m.index));
    add(start, start + inner.length, open === '《' ? '标题' : open === '`' ? '代码' : named ? '命名' : '引用', open === '《' ? 94 : open === '`' ? 90 : named ? 96 : 82);
  });

  /* links, mail, paths, files, versions, commands */
  each(/\bhttps?:\/\/[^\s，。！？；）」”"'<>]+|\bwww\.[A-Za-z0-9-]+\.[^\s，。！？；）」”"'<>]+/g, (m) => {
    const v = m[0].replace(/[.,)]+$/, '');
    add(m.index, m.index + v.length, '链接', 88);
  });
  each(/(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g, (m) => add(m.index, m.index + m[0].length, '邮箱', 86));
  each(/(?<![\w@])@[A-Za-z0-9_\p{Script=Han}-]{2,20}/gu, (m) => add(m.index, m.index + m[0].length, '人物', 80));
  each(/\b[A-Za-z]:[\\/][^\s，。！？；"“”<>|]+|(?<![\w:/.\\-])(?:~|\.{1,2})?\/?(?:[A-Za-z0-9_.@-]+\/)+[A-Za-z0-9_.@-]+/g, (m) => {
    const v = m[0].replace(/[.,]+$/, '');
    const slashes = (v.match(/[\\/]/g) ?? []).length;
    if (!/[A-Za-z]/.test(v) || !(/^[A-Za-z]:/.test(v) || /\.[A-Za-z0-9]{1,6}$/.test(v) || slashes >= 2 || /^(?:~|\.{1,2})?\//.test(v))) return;
    add(m.index, m.index + v.length, '文件', 88);
  });
  each(/(?<![\w./\\-])[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:ts|tsx|js|jsx|mjs|cjs|py|rs|go|java|kt|swift|cpp|cc|c|h|hpp|cs|rb|php|vue|svelte|json|jsonl|md|txt|csv|tsv|xlsx|xls|docx|doc|pptx|ppt|pdf|png|jpe?g|gif|webp|svg|html?|css|scss|less|ya?ml|toml|ini|cfg|conf|log|exe|msi|dll|zip|7z|rar|tar|gz|sh|ps1|bat|cmd|sql|db|ipynb|onnx|wav|mp3|m4a|mp4|mov)(?![A-Za-z0-9])/gi, (m) => {
    if (/^[A-Z][a-z0-9]*\.js$/.test(m[0])) return add(m.index, m.index + m[0].length, '术语', 86); // Node.js, Vue.js
    add(m.index, m.index + m[0].length, '文件', 86);
  });
  each(/(?<![A-Za-z0-9.])[vV]\d+(?:\.\d+){0,3}(?:-[0-9A-Za-z.]+)?(?![\w.])/g, (m) => add(m.index, m.index + m[0].length, '版本', 90));
  each(/(?<![A-Za-z0-9.])([A-Za-z][A-Za-z+#]*)\s?(\d+(?:\.\d+)*)(?![\w.])/g, (m) => {
    const name = m[1].toLowerCase();
    if (TECH.has(name) || (/[A-Z]/.test(m[1]) && m[2].includes('.') && !EN_FILLER.has(name))) add(m.index, m.index + m[0].length, '版本', 92);
  });
  each(/(?<![\w.])\d+(?:\.\d+)+\s?版本?|(?<=版本号?(?:是|为)?\s?)\d+(?:\.\d+)+|(?<![\w.])\d+\.\d+\.\d+(?![\w.])/gu, (m) => add(m.index, m.index + m[0].length, '版本', 86));
  each(/(?<![\w./-])(?:pnpm|npm|npx|yarn|bun|git|gh|pip3?|uv|poetry|conda|python3?|cargo|rustup|go|docker|kubectl|helm|brew|winget|choco|scoop|node|deno|make|cmake|gradle|mvn|dotnet|curl|wget|ssh|tsc|vite|vitest|pytest|ffmpeg)(?:[ \t]+(?:--?[\w-]+(?:=[\w./:@-]+)?|[\w@./:=-]+)){1,4}/g, (m) => {
    const tokens = [...m[0].matchAll(/\S+/g)];
    let n = 1; // stop before prose ("git is great") or a version ("python 3.12")
    while (n < tokens.length && !EN_FILLER.has(tokens[n][0].toLowerCase()) && !/^\d+(?:\.\d+)*$/.test(tokens[n][0])) n++;
    const known = /^(?:install|i|add|remove|rm|run|test|build|dev|push|pull|commit|clone|checkout|switch|status|log|diff|merge|rebase|init|start|stop|exec|up|down|ps|logs|serve|lint|format|check|update|upgrade|publish|deploy|login|apply|get|create|-.+)$/;
    if (n < 2 || (!han && !known.test(tokens[1][0]))) return;
    add(m.index, m.index + tokens[n - 1].index! + tokens[n - 1][0].length, '命令', 92);
  });

  /* English terms and proper nouns */
  each(/[A-Za-z][A-Za-z0-9_./:+#-]*/g, (m) => {
    const word = m[0].replace(/[./:-]+$/, '');
    const lower = word.toLowerCase();
    if (word.length < 2 || EN_FILLER.has(lower) || EN_TIME.has(lower)) return;
    const midSentence = /[a-z,]\s*$/.test(text.slice(Math.max(0, m.index - 4), m.index));
    if (/[A-Z0-9_.+#-]/.test(word.slice(1)) || (TECH.has(lower) && (han || /^[A-Z]/.test(word))) || (/^[A-Z][a-z]+$/.test(word) && (han || midSentence)))
      add(m.index, m.index + word.length, '术语', 86);
  });
  each(STAT_RE, (m) => add(m.index, m.index + m[0].length, '数据', 94));

  /* money, ratios, quantities */
  each(MONEY_RE, (m) => {
    // "五块" may be five pieces; "两千块" is money.
    if (m[1] && !(m[2] === '块' ? weighty(m[1]) : wellFormed(m[1]))) return;
    add(m.index, m.index + m[0].length, '金额', 92);
  });
  // A bare number shortly after a money word ("预算一晚六百以内", "销售额是三百二十万").
  each(MONEY_CTX_RE, (m) => {
    const from = m.index + m[0].length;
    const window = text.slice(from, from + 24).split(/[，。！？；\n,!?;]/)[0];
    for (const n of window.matchAll(MONEY_NUM_RE)) {
      if (n.index! > 6) break;
      if (weighty(n[1])) add(from + n.index!, from + n.index! + n[0].length, '金额', 90);
    }
  });
  each(RATIO_RE, (m) => { if (!m[1] || wellFormed(m[1])) add(m.index, m.index + m[0].length, '比例', 90); });
  each(DISCOUNT_RE, (m) => add(m.index, m.index + m[0].length, '折扣', 88));
  each(DUR_RE, (m) => {
    const [, over, n, unit, , suffix = ''] = m;
    const num = n.replace(/[个多半]/g, '');
    if (/^\d{4}$/.test(num) && unit === '年') return; // a year, handled as a date
    if (num && !wellFormed(num)) return;
    if (!suffix && !over && /^(?:一|1)$/.test(num) && /^(?:天|年|秒|周)$/.test(unit)) return; // "有一天", "一年" are vague
    const label = /内/.test(suffix) ? '期限' : (suffix && suffix !== '左右') || over ? '时间' : '时长';
    add(m.index, m.index + m[0].length, label, label === '期限' ? 100 : label === '时间' ? 84 : 74);
  });
  each(MEAS_RE, (m) => { if (wellFormed(m[1]) && !/^一\s?度$/.test(m[0])) add(m.index, m.index + m[0].length, '数量', 80); });
  each(ASCII_QTY_RE, (m) => add(m.index, m.index + m[0].length, '数量', 80));
  each(COUNT_RE, (m) => {
    const n = m[1].replace(/\s/g, '');
    if (/^\d+$/.test(n) ? Number(n) < 10 && !m[0].includes('多') : !weighty(n)) return; // "3个", "两个" are not facts
    add(m.index, m.index + m[0].length, '数量', 70);
  });
  each(BIG_RE, (m) => { if (wellFormed(m[0].replace(/[万亿]$/, ''))) add(m.index, m.index + m[0].length, '数字', 76); });

  /* times, dates, ranges, deadlines */
  each(TIME_RE, (m) => {
    const [all, pre, point, to, suf = ''] = m;
    const day = IS_DAY.test(point);
    const rest = point.replace(IS_DAY, '');
    const part = HAS_PART.test(rest), clock = HAS_CLOCK.test(rest);
    const deadline = /^(?:之前|以前|前|为止|截止|以内|之内|内)$/.test(suf);
    const cue = !!(pre || to || suf);
    if (!day && !part) { // a bare clock: "三点" may be "三点建议"
      if (/[这那几有第些]$/.test(text.slice(Math.max(0, m.index - 1), m.index))) return;
      if (!/[钟半整分刻:：]|[aApP][mM]/.test(point) && !cue && !CLOCK_CUE.test(text.slice(m.index + all.length, m.index + all.length + 4))) return;
      if (/^[一1]点$/.test(point) && !cue) return;
    } else if (!day && part && !clock && !cue) return; // a bare "下午"
    else if (day && !part && !clock) {
      if ((IS_NOW.test(point) || IS_RECUR.test(point)) && !deadline && !pre) return;
    }
    const past = IS_PAST.test(point) && !to;
    // "deadline 是 10月15日", "截止日期：周五"
    const due = /(?:deadline|ddl|due|截止日期|截止时间|最后期限|期限)\s*(?:是|为|定在|[:：])?\s*$/i.test(text.slice(Math.max(0, m.index - 14), m.index));
    const label = pre || due || (deadline && !past) ? '期限' : '时间';
    const strong = clock || IS_DATE.test(point);
    const score = label === '期限' ? 100 : strong ? 88 : part ? 84 : past ? 60 : IS_FUT.test(point) && VAGUE.test(point) ? 72 : 82;
    add(m.index, m.index + all.length, label, score);
  });
  each(EVENT_DEADLINE_RE, (m) => add(m.index, m.index + m[0].length, '期限', 92));
  each(EN_TIME_RE, (m) => add(m.index, m.index + m[0].length, m[1] ? '期限' : '时间', m[1] ? 100 : 82));

  /* people with a title, organisations, places */
  each(PERSON_RE, (m) => { if (boundary(m.index) && boundary(m.index + m[0].length)) add(m.index, m.index + m[0].length, '人物', 76); });
  const segAt = new Map(segs.map((s) => [s.index + s.segment.length, s])); // by end offset
  const prefixTokens = (end: number, maxChars: number) => {
    // The suffix may be glued into one dictionary word ("清华大学").
    if (!bounds.has(end) && segs.length) {
      const word = segs.find((s) => s.index < end && s.index + s.segment.length > end)!;
      return /^\p{Script=Han}+$/u.test(word.segment) && !STOP.has(word.segment) && end - word.index <= maxChars ? word.index : end;
    }
    let start = end;
    for (let step = 0; step < 2; step++) {
      const s = segAt.get(start);
      if (!s) break;
      const w = s.segment;
      if (!/^\p{Script=Han}+$/u.test(w) || STOP.has(w) || FUNC.has(w) || /^[零〇一二两三四五六七八九十百千万第]+$/u.test(w) || IS_ACT.test(w) || end - s.index > maxChars) break;
      start = s.index;
    }
    return start;
  };
  each(ORG_RE, (m) => {
    const start = prefixTokens(m.index, 6);
    if (m.index - start >= 2) add(start, m.index + m[0].length, '机构', 78);
  });
  each(PLACE_RE, (m) => {
    const floor = text.slice(Math.max(0, m.index - 8), m.index).match(/(?:[零一二两三四五六七八九十\d]+(?:楼|层|号楼)|[A-Za-z]?\d{1,4}(?:室)?)$/u);
    const start = floor ? m.index - floor[0].length : prefixTokens(m.index, 5);
    if (m.index - start >= 2) add(start, m.index + m[0].length, '地点', 74);
  });
  each(CITY_RE, (m) => { if (boundary(m.index) && boundary(m.index + m[0].length)) add(m.index, m.index + m[0].length, '地点', 74); });

  /* clause-level: constraints, requirements, open points, decisions, actions, ideas, questions */
  const anchors: Cand[][] = clauses.map(() => []); // time/deadline/owner atoms per clause
  for (const a of cands) if (/时间|期限|人物/.test(a.label) && clauseAt[a.start] >= 0) anchors[clauseAt[a.start]].push(a);
  // Cue words, found once over the whole text and filed by clause: [cue, start, length].
  const hits: [number, number, number][][] = clauses.map(() => []);
  CUES.forEach((cue, k) => {
    if (cue.re.source.startsWith('^')) {
      clauses.forEach((c, ci) => { const m = text.slice(c.start, c.end).match(cue.re); if (m) hits[ci].push([k, c.start, m[0].length]); });
    } else {
      for (const m of text.matchAll(cue.re)) {
        const ci = clauseAt[m.index!];
        if (ci >= 0 && m.index! + m[0].length <= clauses[ci].end) hits[ci].push([k, m.index!, m[0].length]);
      }
    }
  });
  clauses.forEach((c, ci) => {
    const body = text.slice(c.start, c.end);
    // Live preview text often has no punctuation: inside a long run, a new owner, time,
    // cue word or subject starts a new thought ("…新版本|张伟负责测试|月底前要…").
    soft = [];
    for (const [, at] of hits[ci]) if (at > c.start) soft.push(at);
    if (ink(body) > 20) {
      for (const a of anchors[ci]) if (a.start > c.start && a.end <= c.end) soft.push(a.start);
      for (const m of body.matchAll(/我们|你们|他们|大家|咱们/gu)) if (m.index) soft.push(c.start + m.index);
    }
    soft.sort((a, b) => a - b);
    for (const [k, at, len] of hits[ci]) {
      const cue = CUES[k];
      const from = cue.keep ? at : at + len;
      const span = phrase(from, c.end, cue.min ?? (cue.keep ? ink(text.slice(at, at + len)) + 2 : 2), cue.label === '设想' ? 18 : 16);
      if (span) add(span[0], span[1], cue.label, cue.score, false, at);
    }
    // Imperatives at the clause start, also right after a leading time/owner ("周五前整理数据").
    const starts = [c.start, ...soft];
    for (const a of anchors[ci]) if (a.end <= c.end && (a.start <= c.start + 2 || soft.includes(a.start))) starts.push(a.end);
    for (const s0 of starts) {
      const lead = text.slice(s0, c.end).match(SEQ)?.[0].length ?? 0;
      const from = s0 + lead;
      if (!IMPERATIVE.test(text.slice(from, c.end))) continue;
      const span = phrase(from, c.end, 4, 16);
      if (!span || STATEMENT.test(text.slice(span[0], span[1]))) continue;
      add(span[0], span[1], '待办', 84, false, s0);
    }
    // Questions: an explicit "？", a closing 吗/呢, or an A-not-A form.
    const trimmed = body.replace(/[\s啊呀吧嘛哦]+$/u, '');
    const head = body.replace(LEAD_FILLER, '');
    const asked = /[?？]/.test(c.sep) || /吗$/u.test(trimmed) || (/[呢么]$/u.test(trimmed) && QWORD.test(trimmed)) || ANOTA.test(head.slice(0, 3));
    if (asked && !QFILLER.test(trimmed.replace(LEAD_FILLER, ''))) {
      const q = body.search(QWORD);
      const lead = body.length - head.length;
      const from = c.start + (q > lead && q - lead > 6 ? q : lead);
      const span = phrase(from, c.end, 4, 16, /(?:\s|[啊呀吧嘛哦哈啦])+$/u);
      if (span) add(span[0], span[1], '问题', 92, false);
    }
    // "结论：……" / "待办：……"
    const header = /[：:]/.test(c.sep) ? body.match(HEADER) : null;
    const next = clauses[ci + 1];
    if (header && next) {
      soft = [];
      const span = phrase(next.start, next.end, 3, 18);
      if (span) add(span[0], span[1], HEADER_LABEL[header[1]], 100, false);
    }
  });

  /* key phrases (TextRank over segmenter words) */
  // Words already explained by a fact (a time, an amount, a person…) are not key phrases.
  const covered = new Uint8Array(text.length + 1);
  for (const f of cands) if (f.atom) covered.fill(1, f.start, f.end);
  keyPhrases(text, segs, clauseAt, covered, (s, e, score) => add(s, e, '要点', score, false));

  return { cands: refine(text, cands, clauseAt), clauses, clauseAt };
}

/** Attach an owner/deadline to its action, prefer a specific atom over a wrapper, drop duplicates. */
function refine(text: string, cands: Cand[], clauseAt: Int32Array) {
  const out = cands.slice();
  const anchorsByEnd = new Map<number, Cand[]>();
  for (const a of cands) if (a.atom && /时间|期限|人物/.test(a.label)) anchorsByEnd.set(a.end, [...(anchorsByEnd.get(a.end) ?? []), a]);
  for (const c of cands) {
    if (c.atom || !/待办|结论/.test(c.label)) continue;
    // The owner/deadline must sit right before the cue ("王经理|负责…", "月底前|要把…").
    let start = c.lead, score = c.score, merged = false;
    for (let step = 0; step < 2; step++) {
      let before: Cand | undefined;
      for (let e = start; e >= 0 && (e === start || /\s/.test(text[e])); e--)
        for (const a of anchorsByEnd.get(e) ?? []) if (!before || a.start < before.start) before = a;
      if (!before || ink(text.slice(before.start, c.end)) > 16) break;
      start = before.start;
      score = Math.max(score, before.score) + 2;
      merged = true;
    }
    if (merged) out.push({ ...c, start, score, lead: start });
  }
  // A wrapper that is mostly one fact shows the fact ("跑一下 pnpm test" → 命令 pnpm test).
  const atomsByClause = new Map<number, Cand[]>();
  for (const a of out) if (a.atom) atomsByClause.set(clauseAt[a.start], [...(atomsByClause.get(clauseAt[a.start]) ?? []), a]);
  for (const c of out) {
    if (c.atom) continue;
    const weight = ink(text.slice(c.start, c.end));
    const inner = (atomsByClause.get(clauseAt[c.start]) ?? []).some((a) => a.start >= c.start && a.end <= c.end && ink(text.slice(a.start, a.end)) / weight >= .55);
    if (inner) c.score -= 25;
  }
  const best = new Map<string, Cand>();
  for (const c of out) {
    const key = `${c.start}:${c.end}`;
    const prev = best.get(key);
    if (!prev || c.score > prev.score) best.set(key, c);
  }
  // A mark nested inside a longer same-label mark adds nothing ("一百九十九" inside "一百九十九元").
  const unique = [...best.values()].sort((a, b) => (a.label < b.label ? -1 : a.label > b.label ? 1 : 0) || a.start - b.start || b.end - a.end);
  const kept: Cand[] = [];
  let label = '', reach = -1;
  for (const c of unique) {
    if (c.label !== label) { label = c.label; reach = -1; }
    if (c.end <= reach) continue;
    kept.push(c);
    reach = c.end;
  }
  return kept;
}

/** A small TextRank: words co-occurring with many others in the dictation rank higher. */
function keyPhrases(text: string, segs: Seg[], clauseAt: Int32Array, covered: Uint8Array,
  push: (start: number, end: number, score: number) => void) {
  type Tok = { w: string; i: number; end: number; kind: 'node' | 'generic' | 'single' | 'stop'; clause: number };
  const toks: Tok[] = [];
  for (const s of segs) {
    if (!s.isWordLike || !/^\p{Script=Han}+$/u.test(s.segment)) continue;
    const w = s.segment;
    const end = s.index + w.length;
    // Numbers, pronouns/deictics ("我在", "这边"), degree words ("很好"), "看一下", laughter ("哈哈").
    const kind = STOP.has(w) || covered.subarray(s.index, end).includes(1) || /^[零〇一二两三四五六七八九十百千万亿几第半每我你他她它咱这那哪某很太挺真好]|一下$|^(.)\1+$/u.test(w) ? 'stop'
      : w.length === 1 ? (FUNC.has(w) ? 'stop' : 'single') : GENERIC.has(w) ? 'generic' : 'node';
    toks.push({ w, i: s.index, end, kind, clause: clauseAt[s.index] });
  }
  const nodes = toks.filter((t) => t.kind === 'node' || t.kind === 'generic');
  if (nodes.filter((t) => t.kind === 'node').length < 2) return;
  // Co-occurrence graph within each clause, window 3; PageRank, 30 rounds.
  const id = new Map<string, number>();
  for (const t of nodes) if (!id.has(t.w)) id.set(t.w, id.size);
  const edges = new Map<number, number>(); // a * size + b → weight
  const size = id.size;
  nodes.forEach((t, k) => {
    for (let j = k + 1; j < Math.min(nodes.length, k + 4); j++) {
      const a = id.get(t.w)!, b = id.get(nodes[j].w)!;
      if (nodes[j].clause !== t.clause || a === b) continue;
      edges.set(a * size + b, (edges.get(a * size + b) ?? 0) + 1);
      edges.set(b * size + a, (edges.get(b * size + a) ?? 0) + 1);
    }
  });
  const from = new Int32Array(edges.size), to = new Int32Array(edges.size), weight = new Float64Array(edges.size);
  const total = new Float64Array(size);
  let e = 0;
  for (const [key, w] of edges) { from[e] = Math.floor(key / size); to[e] = key % size; weight[e] = w; total[from[e]] += w; e++; }
  let rankOf = new Float64Array(size).fill(1);
  for (let iteration = 0; iteration < 30; iteration++) {
    const next = new Float64Array(size).fill(.15);
    for (let k = 0; k < e; k++) next[to[k]] += .85 * (weight[k] / total[from[k]]) * rankOf[from[k]];
    rankOf = next;
  }
  const rank = new Map([...id].map(([w, i]) => [w, rankOf[i]]));
  // Phrases: runs of adjacent content tokens; a run never starts with a single character.
  const phrases = new Map<string, { start: number; end: number; score: number; count: number; long: boolean }>();
  let run: Tok[] = [];
  const flush = () => {
    while (run.length && run[0].kind === 'single') run.shift();
    for (let a = 0; a < run.length; a++) {
      if (run[a].kind === 'single') continue;
      for (let b = a; b < Math.min(run.length, a + 4); b++) {
        const part = run.slice(a, b + 1);
        const value = text.slice(part[0].i, part[part.length - 1].end);
        const len = [...value].length;
        if (len < 3 || len > 8 || !part.some((t) => t.kind === 'node')) continue;
        // Prefer noun phrases ("语音可懂度") over verb phrases ("分开测量"); no POS tagger, just their tails.
        const last = part[part.length - 1].w;
        const shape = NOUN_TAIL.test(last) ? 1.3 : IS_ACT.test(last) || VERB_TAIL.has(last) ? .55 : 1;
        const raw = part.reduce((n, t) => n + (t.kind === 'node' ? rank.get(t.w) ?? 0 : t.kind === 'generic' ? (rank.get(t.w) ?? 0) * .3 : 0), 0) * (1 + .08 * (len - 3)) * shape;
        const seen = phrases.get(value);
        if (seen) seen.count++;
        else phrases.set(value, { start: part[0].i, end: part[part.length - 1].end, score: raw, count: 1, long: part.some((t) => t.kind === 'node' && t.w.length >= 3) });
      }
    }
    run = [];
  };
  for (const t of toks) {
    if (t.kind === 'stop' || (run.length && run[run.length - 1].end !== t.i)) flush();
    if (t.kind !== 'stop') run.push(t);
  }
  flush();
  const scored = [...phrases.entries()].map(([value, p]) => ({ value, ...p, total: p.score * Math.sqrt(p.count) }));
  const top = scored.reduce((n, p) => Math.max(n, p.total), 0);
  if (!top) return;
  for (const p of scored) {
    const len = [...p.value].length;
    if (len < 4 && !(p.count > 1 || p.long)) continue;
    push(p.start, p.end, 46 + 16 * (p.total / top));
  }
}

/* ── selection ────────────────────────────────────────────────────────── */

export function keywords(text: string, limit = 6): Mark[] {
  if (limit <= 0 || !text.trim()) return [];
  const { cands, clauses, clauseAt } = analysis(text);
  const size = ink(text);
  const cap = Math.min(limit, Math.max(2, Math.ceil(size / 14)));
  const budget = size <= 40 ? 24 : size <= 80 ? 24 + (size - 40) * .175 : size <= 170 ? 31 + (size - 80) * 7 / 90 : Math.max(38, size * .22);
  const keyCap = size < 50 ? 1 : size < 120 ? 2 : 3;
  const longRun = new Set(clauses.flatMap((c, i) => (ink(text.slice(c.start, c.end)) > 24 ? [i] : [])));
  const pool = cands.map((c) => ({ ...c, clause: clauseAt[c.start], weight: ink(text.slice(c.start, c.end)), key: text.slice(c.start, c.end).toLowerCase() }));
  const chosen: typeof pool = [];
  const seen = new Set<string>();
  let used = 0, keys = 0;
  while (chosen.length < cap) {
    let best: typeof pool[number] | null = null, bestScore = -Infinity;
    for (const c of pool) {
      if (seen.has(c.key) || used + c.weight > budget || (c.label === '要点' && keys >= keyCap)) continue;
      if (chosen.some((s) => c.start < s.end && c.end > s.start)) continue;
      // Spread emphasis over the utterance; a second mark in a clause must be clearly worth it.
      // A long unpunctuated run counts as several clauses once marks are far enough apart.
      const crowd = chosen.filter((s) => s.clause === c.clause &&
        (!longRun.has(c.clause) || Math.max(c.start - s.end, s.start - c.end) < 10)).length;
      const score = c.score - 30 * crowd;
      if (score > bestScore || (score === bestScore && best && (c.start < best.start || (c.start === best.start && c.end > best.end)))) { best = c; bestScore = score; }
    }
    if (!best || bestScore < 50) break;
    chosen.push(best);
    seen.add(best.key);
    used += best.weight;
    if (best.label === '要点') keys++;
  }
  return chosen.sort((a, b) => a.start - b.start).map(({ start, end, label }) => ({ start, end, label }));
}

/** Split text into plain and emphasized runs for rendering. */
export function runs(text: string, limit = 7) {
  const marks = keywords(text, limit);
  const parts: { text: string; mark?: Mark }[] = [];
  let offset = 0;
  for (const mark of marks) {
    if (mark.start > offset) parts.push({ text: text.slice(offset, mark.start) });
    parts.push({ text: text.slice(mark.start, mark.end), mark });
    offset = mark.end;
  }
  if (offset < text.length) parts.push({ text: text.slice(offset) });
  return parts;
}

/* ── tag suggestion ───────────────────────────────────────────────────── */

/* strong cue = 3, weak cue = 1 (distinct words), plus structural evidence from the
   marks above (`core` labels alone make a tag eligible, e.g. an action item → 待办). */
interface TagRule { tag: string; strong: RegExp; weak: RegExp; marks: Record<string, number>; core?: string[] }
const TAGS: TagRule[] = [
  { tag: '待办', strong: /待办|todo|TODO|任务清单|别忘了|别忘记|记得|提醒我/g, weak: /截止|deadline|ddl|要做|安排|负责|跟进|提交|完成/g, marks: { 待办: 2, 期限: 1 }, core: ['待办'] },
  { tag: '会议', strong: /会议|开会|例会|周会|晨会|评审会|纪要|议程|与会|参会|复盘会/g, weak: /讨论|汇报|同步|对齐|决定|拍板|老板|客户|领导|议题|会上/g, marks: { 结论: 1, 时间: 1 } },
  { tag: '论文', strong: /论文|文献|期刊|审稿|投稿|综述|被试|样本|显著性?|回归|方差|因果|假设检验|实验组|对照组|效应量?|数据集|引用/g, weak: /实验|假设|变量|模型|摘要|导师|课题|研究|理论|机制|相关性/g, marks: { 数据: 2, 推测: 1, 待定: 1 }, core: ['数据'] },
  { tag: '技术', strong: /代码|报错|[Bb]ug|BUG|接口|函数|组件|部署|编译|依赖|重构|前端|后端|数据库|服务器|仓库|分支|commit|API|SDK/g, weak: /测试|版本|上线|配置|脚本|性能|日志|调试|修复|开发/g, marks: { 命令: 2, 文件: 2, 代码: 2, 版本: 1, 术语: 1 }, core: ['命令', '文件', '代码'] },
  { tag: '灵感', strong: /灵感|点子|创意|脑洞|突发奇想|有个想法|想到一个/g, weak: /想法|如果能|要是能|不如|或许|也许|设想|可以做/g, marks: { 设想: 2 }, core: ['设想'] },
  { tag: '出行', strong: /出差|机票|航班|高铁|火车票|酒店|签证|登机|行程/g, weak: /打车|订票|旅游|出发|住在/g, marks: { 地点: 1, 时间: 1 } },
  { tag: '学习', strong: /作业|考试|复习|课程|网课|背单词|讲座|上课|读书笔记/g, weak: /学习|读书|笔记|阅读|章节|老师/g, marks: { 标题: 1 } },
  { tag: '财务', strong: /报销|发票|账单|转账|工资|理财|预算|付款|房贷|记账/g, weak: /费用|价格|成本|收入|支出|花了/g, marks: { 金额: 1 } },
  { tag: '生活', strong: /房租|水电|快递|超市|买菜|做饭|体检|挂号|健身/g, weak: /妈妈|爸爸|家里|孩子|老婆|老公|吃饭|火锅|周末/g, marks: {} },
];

/** A tag suggestion from the note text; the user accepts it with one click. */
export function suggestTag(text: string, existing: string[]) {
  if (!text.trim()) return null;
  const have = existing.map((t) => t.trim()).filter(Boolean);
  const labels = new Map<string, number>();
  for (const c of analysis(text).cands) if (c.score >= 60) labels.set(c.label, (labels.get(c.label) ?? 0) + 1);
  let best: string | null = null, bestScore = 3; // a single strong word needs one more cue
  for (const rule of TAGS) {
    if (have.some((t) => t === rule.tag || t.includes(rule.tag) || rule.tag.includes(t))) continue;
    const strong = new Set(text.match(rule.strong) ?? []).size;
    const weak = new Set(text.match(rule.weak) ?? []).size;
    // Conservative: needs its own words or a direct structural cue, then enough total support.
    if (!strong && weak < 2 && !(rule.core ?? []).some((label) => labels.has(label))) continue;
    let score = strong * 3 + Math.min(weak, 3);
    for (const [label, weight] of Object.entries(rule.marks)) score += Math.min(2, labels.get(label) ?? 0) * weight;
    if (score > bestScore) { best = rule.tag; bestScore = score; }
  }
  return best;
}
