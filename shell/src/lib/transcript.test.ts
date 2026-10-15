import { describe, expect, it, vi } from 'vitest';
import { keywords, runs, suggestTag, type Mark } from './transcript';

const marked = (text: string, limit?: number) => keywords(text, limit).map((m) => text.slice(m.start, m.end));
const labelled = (text: string, limit?: number) => keywords(text, limit).map((m) => `${m.label}:${text.slice(m.start, m.end)}`);
const labelOf = (text: string, phrase: string, limit = 7) => keywords(text, limit).find((m) => text.slice(m.start, m.end) === phrase)?.label;

const LABELS = new Set(['约束', '要求', '结论', '待定', '推测', '待办', '期限', '时间', '时长', '问题', '设想', '金额', '比例', '折扣',
  '数字', '数量', '数据', '版本', '命令', '文件', '链接', '邮箱', '代码', '术语', '标题', '命名', '引用', '人物', '机构', '地点', '要点']);

/** Structural guarantees every caller (VoiceScore brackets, Notes runs) relies on. */
function wellFormedMarks(text: string, marks: Mark[]) {
  marks.forEach((m, i) => {
    expect(m.start).toBeGreaterThanOrEqual(0);
    expect(m.end).toBeLessThanOrEqual(text.length);
    expect(m.end).toBeGreaterThan(m.start);
    if (i) expect(m.start).toBeGreaterThanOrEqual(marks[i - 1].end); // sorted, never overlapping
    const value = text.slice(m.start, m.end);
    expect(value).toBe(value.trim());
    expect(value).not.toMatch(/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/); // never splits a surrogate pair
    expect(LABELS.has(m.label)).toBe(true);
    expect([...m.label]).toHaveLength(2); // fits above a word in VoiceScore
  });
}

describe('conservative transcript emphasis (original guarantees)', () => {
  it('keeps the negative with its action', () => {
    expect(marked('帮我修复输入位置，不要自动发送。')).toContain('不要自动发送');
    expect(marked('帮我修复输入位置，不要自动发送。')).not.toContain('发送');
  });
  it('retains scientific uncertainty', () => {
    expect(marked('可能存在交互效应，尚未验证因果关系。')).toEqual(['可能存在交互效应', '尚未验证因果关系']);
    expect(labelled('可能存在交互效应，尚未验证因果关系。')).toEqual(['推测:可能存在交互效应', '待定:尚未验证因果关系']);
  });
  it('does not mark fillers or bare sequence numbers', () => {
    expect(marked('嗯，就是说，我觉得，然后呢，1、2、3。')).toEqual([]);
    expect(marked('please do this and that, okay')).toEqual([]);
  });
  it('marks complete actions and named technical terms', () => {
    expect(marked('修复自动滚动，使用 TypeScript。')).toEqual(['修复自动滚动', 'TypeScript']);
  });
  it('keeps quantities and units together', () => {
    expect(marked('耗时 150 ms，参与者 120人。')).toEqual(['150 ms', '120人']);
  });
  it('keeps a scheduled time together', () => {
    expect(marked('明天下午三点，讨论方案。')).toContain('明天下午三点');
  });
  it('deduplicates repeated names and does not highlight an English sentence', () => {
    expect(marked('TypeScript，TypeScript，TypeScript。')).toEqual(['TypeScript']);
    expect(marked('Please save the file and then close it.')).toEqual([]);
    expect(marked('Today I want to say something about this.')).toEqual([]);
  });
  it('preserves all source characters and emoji offsets', () => {
    const text = '💡 修复自动滚动。\n不要自动发送，记录耗时 150 ms。';
    expect(runs(text).map((r) => r.text).join('')).toBe(text);
    expect(marked(text)).toContain('不要自动发送');
    for (const m of keywords(text)) expect(text.slice(m.start, m.end)).not.toMatch(/[\uD800-\uDFFF]/u);
  });
  it('caps density and includes a later requirement', () => {
    const text = ('今天我想先说一下背景情况，这个问题以前出现过，我们当时讨论过其他方案。').repeat(4) +
      'TypeScript 的界面需要调整。不要自动发送，必须保留原始文字。';
    const marks = keywords(text);
    expect(marks.length).toBeLessThanOrEqual(6);
    expect(marks.reduce((n, m) => n + m.end - m.start, 0)).toBeLessThanOrEqual(Math.floor(text.length * .22));
    expect(marked(text)).toContain('必须保留原始文字');
    expect(marks.every((m, i) => !i || m.start >= marks[i - 1].end)).toBe(true);
    // The filler-heavy background never earns a bracket.
    expect(marks.every((m) => m.start >= text.indexOf('TypeScript'))).toBe(true);
  });
  it('supports an explicit zero limit', () => expect(keywords('不要自动发送', 0)).toEqual([]));
});

describe('meeting notes', () => {
  it('keeps the owner with the task, the time and the budget', () => {
    const text = '明天下午三点在三楼会议室开项目例会，王经理负责准备预算表，预算控制在5万元以内。';
    expect(labelled(text, 3)).toEqual(['时间:明天下午三点', '待办:王经理负责准备预算表', '金额:5万元以内']);
    expect(suggestTag(text, [])).toBe('会议');
  });
  it('marks decisions, deadline-bound action items and money in one pass', () => {
    const text = '我们决定下周三上线新版本，张伟负责测试，月底前要把用户反馈整理出来。另外服务器费用每月大概两千块，涨了百分之十五。';
    const marks = labelled(text, 3);
    expect(marks).toContain('结论:决定下周三上线新版本');
    expect(marks).toContain('待办:月底前要把用户反馈整理出来');
    expect(labelOf(text, '两千块')).toBe('金额');
    expect(labelled('服务器费用涨了百分之十五。')).toEqual(['比例:百分之十五']);
  });
  it('reads amounts from context and skips the small talk', () => {
    const text = '嗯，今天这个会主要是说一下，就是说，上个季度的销售额是三百二十万，比去年同期增长了12%，然后呢，下个季度目标定在四百万。';
    expect(labelled(text, 3)).toEqual(['金额:三百二十万', '比例:12%', '数字:四百万']);
    expect(marked(text, 7)).not.toContain('主要是');
  });
  it('lists several action items and drops the reminder words', () => {
    const text = '待会儿需要把合同发给法务，然后安排李老师下周一和客户见面，还有别忘了交房租。';
    expect(labelled(text)).toEqual(['待办:把合同发给法务', '待办:安排李老师下周一和客户见面', '待办:交房租']);
    expect(suggestTag(text, [])).toBe('待办');
  });
  it('turns "结论：…" headers into a decision', () => {
    expect(labelled('结论：方案A更稳定')).toEqual(['结论:方案A更稳定']);
    expect(marked('这个结论是对的')).toEqual([]);
    expect(labelled('我还没决定')).toEqual(['待定:还没决定']);
    expect(labelled('经过讨论，最终确定采用方案B，原因是成本低、周期短。')).toContain('结论:最终确定采用方案B');
  });
});

describe('research notes', () => {
  it('marks statistics, the conclusion and what is still open', () => {
    const text = '这篇论文的核心假设是注意力分配会影响记忆编码，实验一共招募了48名被试，p<0.05，效应量d=0.62，结论是假设成立，但还需要重复实验验证。';
    const marks = labelled(text, 7);
    expect(marks).toEqual(expect.arrayContaining(['数据:p<0.05', '数据:d=0.62', '结论:假设成立', '待定:还需要重复实验验证']));
    expect(labelled(text, 3)).toEqual(expect.arrayContaining(['结论:假设成立', '待定:还需要重复实验验证']));
    expect(suggestTag(text, [])).toBe('论文');
  });
  it('offers a key phrase only for a specific noun phrase', () => {
    const text = '让可见性与语音可懂度分开测量。\n可能存在交互效应，样本 n=120，不直接认定为创新。';
    expect(labelled(text, 3)).toEqual(['要点:语音可懂度', '推测:可能存在交互效应', '数据:n=120']);
  });
  it('marks book titles and ideas', () => {
    const text = '读《思考，快与慢》的时候想到，系统一和系统二的区分也许可以用来解释用户在语音输入时的犹豫。';
    expect(labelOf(text, '思考，快与慢')).toBe('标题');
    expect(marked(text)).not.toContain('读');
  });
});

describe('coding instructions', () => {
  it('prefers the specific fact over the instruction that wraps it', () => {
    const text = '把 useEffect 里的依赖改一下，然后在 src/lib/store.ts 里加一个 resetSession 方法，注意不要破坏现有的测试，记得跑一下 pnpm test。';
    const marks = labelled(text, 7);
    expect(marks).toEqual(expect.arrayContaining(['文件:src/lib/store.ts', '约束:不要破坏现有的测试', '命令:pnpm test']));
    expect(marks).not.toContain('待办:跑一下 pnpm test');
    expect(suggestTag(text, [])).toBe('技术');
  });
  it('marks versions, the question and the rollback target', () => {
    const text = '升级到 Python 3.12 之后 numpy 报错了，是不是版本不兼容？先回滚到 v1.4.2 再看。';
    expect(labelled(text)).toEqual(['版本:Python 3.12', '问题:是不是版本不兼容', '版本:v1.4.2']);
  });
  it('keeps whole paths, URLs and commands', () => {
    expect(labelled('打开 D:\\Projects\\demo\\config.json 看一下')).toEqual(['文件:D:\\Projects\\demo\\config.json']);
    expect(labelled('访问 https://example.com/docs 查看文档')).toEqual(['链接:https://example.com/docs']);
    expect(labelled('git push origin main 然后通知大家')).toEqual(['命令:git push origin main']);
  });
  it('does not mistake a past step for an instruction', () => {
    expect(labelled('升级到 Python 3.12 之后 numpy 报错了')).not.toContain('待办:升级到 Python 3.12');
  });
});

describe('time and dates', () => {
  it.each([
    ['报告最晚周四中午之前交', '期限:最晚周四中午之前'],
    ['三天内给我反馈', '期限:三天内'],
    ['下午两点到四点我都在', '时间:下午两点到四点'],
    ['我们要在十二点之前到', '期限:十二点之前'],
    ['每周一上午十点开例会', '时间:每周一上午十点'],
    ['2024年10月15日发布', '时间:2024年10月15日'],
    ['下个月十号去上海出差', '时间:下个月十号'],
    ['他三年前去过上海', '时间:三年前'],
    ['十分钟后开会', '时间:十分钟后'],
    ['这个 feature 我们下个 sprint 做，deadline 是 10月15日。', '期限:10月15日'],
    ['Can you send me the report by Friday?', '期限:by Friday'],
  ])('%s', (text, expected) => expect(labelled(text)).toContain(expected));
  it('keeps a deadline with its task', () => {
    expect(labelled('周五前整理对照组数据，记录完成时间与错误率。')).toEqual(['待办:周五前整理对照组数据', '待办:记录完成时间与错误率']);
    expect(labelled('两小时后提醒我喝水')).toEqual(['待办:两小时后提醒我喝水']);
  });
  it('does not read "三点" or "一点" as a clock without context', () => {
    expect(marked('这三点建议大家看一下')).toEqual([]);
    expect(marked('我有一点累')).toEqual([]);
    expect(marked('说快一点')).toEqual([]);
    expect(marked('今天天气不错')).toEqual([]);
    expect(marked('这个月的情况不太好')).toEqual([]);
  });
});

describe('numbers', () => {
  it('handles Chinese numerals with units', () => {
    const text = '这台笔重量大概38克，电池300毫安时，续航十个小时，价格一百九十九元，打八折。';
    expect(labelled(text, 7)).toEqual(expect.arrayContaining(['数量:38克', '金额:一百九十九元', '折扣:打八折']));
    expect(marked(text, 7)).not.toContain('九十九');
    expect(labelled('买了三斤苹果花了二十五块')).toEqual(['数量:三斤', '金额:二十五块']);
    expect(labelled('预算三千')).toEqual(['金额:三千']);
    expect(labelled('把音量调到百分之八十')).toEqual(['比例:百分之八十']);
  });
  it('does not treat every count as a fact', () => {
    expect(marked('一共两个人')).toEqual([]);
    expect(marked('长按 ⑦ 在三种模式之间循环。\n说话时便签实时出字，说完再按一次保存。')).toEqual([]);
    expect(marked('他身家千万')).toEqual([]); // 千万 is not a number here
  });
});

describe('people, organisations, places, names', () => {
  it('needs a title or a clear context for people', () => {
    expect(labelled('清华大学的李教授下周来讲座', 7)).toEqual(expect.arrayContaining(['机构:清华大学']));
    expect(labelled('刚才李教授说了，这个结果很有意思。')).toEqual(['人物:李教授']);
    expect(marked('高总是说要开会')).toEqual([]);
    expect(labelled('小程序怎么用？')).toEqual(['问题:小程序怎么用']);
  });
  it('keeps a floor with its room, and cities', () => {
    expect(labelOf('在三楼会议室，十点见。', '三楼会议室')).toBe('地点');
    expect(labelled('我们公司在北京')).toEqual(['地点:北京']);
  });
  it('distinguishes a given name from a quotation', () => {
    expect(labelled('这个功能叫做“随拿随用”')).toEqual(['命名:随拿随用']);
    const text = '为什么识别结果里总是把“点声”识别成“电声”？是不是热词没有生效？';
    expect(labelled(text)).toEqual(['引用:点声', '引用:电声', '问题:是不是热词没有生效']);
  });
});

describe('casual speech and short phrases', () => {
  it('marks the question, never the fillers', () => {
    expect(labelled('嗯那个，我觉得吧，今天天气还挺好的，然后就是说我们要不要去吃火锅啊？')).toEqual(['问题:要不要去吃火锅']);
  });
  it('turns reminders into action items', () => {
    expect(labelled('记得给妈妈打电话，顺便问一下周六几点到家。')).toEqual(['待办:给妈妈打电话', '待办:问一下周六几点到家']);
    expect(labelled('千万别忘了带伞')).toEqual(['待办:带伞']);
  });
  it.each(['好的', '发送', '嗯嗯好的好的', '哈哈哈哈', '对吧', '你知道吗？', '怎么说呢，就是那种感觉', '我要说的是这个问题很重要',
    '你要不要试试', '一会儿见', '测试结果很好', '有一天我在路上', '统一一下格式', '我们讨论过其他方案', '好像有点问题', '第一阶段已经完成了'])(
    'leaves "%s" alone', (text) => expect(marked(text)).toEqual([]));
  it('keeps short commands meaningful', () => {
    expect(labelled('明天八点叫我起床')).toEqual(['时间:明天八点']);
    expect(labelled('他说千万不要迟到')).toEqual(['约束:千万不要迟到']);
  });
});

describe('English and mixed text', () => {
  it('marks identifiers and deadlines, not prose', () => {
    const text = 'Please review the PR before Friday and ping @alice on Slack about the API rate limit.';
    const marks = labelled(text, 3);
    expect(marks).toContain('期限:before Friday');
    expect(marks.every((m) => !/:(?:review|ping|rate|limit|the)$/.test(m))).toBe(true);
  });
  it('marks the action in mixed speech', () => {
    expect(labelled('这个 feature 我们下个 sprint 做，先把 bug 修掉，deadline 是 10月15日。')).toEqual(['待办:把 bug 修掉', '期限:10月15日']);
  });
});

describe('unpunctuated live preview text', () => {
  it('finds thoughts inside one long run', () => {
    const text = '我们决定下周三上线新版本张伟负责测试月底前要把用户反馈整理出来';
    expect(labelled(text, 3)).toEqual(['结论:决定下周三上线新版本', '待办:月底前要把用户反馈整理出来']);
    expect(labelled('帮我修复输入位置不要自动发送', 3)).toEqual(['待办:修复输入位置', '约束:不要自动发送']);
  });
  it('cuts a phrase at a spoken filler or "但"', () => {
    const text = '结论是假设成立但还需要重复实验验证嗯就是说我觉得必须保留原始文字嗯就是说我觉得';
    const marks = marked(text, 7);
    expect(marks).toContain('假设成立');
    expect(marks.every((m) => !/嗯|就是说|我觉得/.test(m))).toBe(true);
  });
});

describe('selection', () => {
  const samples = [
    '明天下午三点在三楼会议室开项目例会，王经理负责准备预算表，预算控制在5万元以内。',
    '把 useEffect 里的依赖改一下，然后在 src/lib/store.ts 里加一个 resetSession 方法，注意不要破坏现有的测试，记得跑一下 pnpm test。',
    '💡 灵感：如果能把录音笔做成一个可以挂在胸前的小圆片，开会的时候就不用拿在手里了。',
    'Please review the PR before Friday and ping @alice on Slack about the API rate limit.',
    ('今天我想先说一下背景情况，这个问题以前出现过。').repeat(10) + '月底前要把用户反馈整理出来。',
  ];
  it('produces well-formed marks for every sample and limit', () => {
    for (const text of samples) for (const limit of [1, 3, 7]) {
      const marks = keywords(text, limit);
      expect(marks.length).toBeLessThanOrEqual(limit);
      wellFormedMarks(text, marks);
    }
  });
  it('spreads marks over clauses', () => {
    const text = samples[0];
    const clauseOf = (i: number) => text.slice(0, i).split(/[，。]/).length;
    const clauses = keywords(text, 3).map((m) => clauseOf(m.start));
    expect(new Set(clauses).size).toBe(clauses.length);
  });
  it('is deterministic and order-independent', () => {
    const first = samples.map((t) => keywords(t, 3));
    const again = [...samples].reverse().map((t) => keywords(t, 3)).reverse();
    expect(again).toEqual(first);
    expect(keywords(samples[0], 7)).toEqual(keywords(samples[0], 7));
  });
  it('finds a late action item in a long, repetitive note', () => {
    expect(marked(samples[4])).toEqual(['月底前要把用户反馈整理出来']);
  });
  it('stays fast on a long note', () => {
    const long = samples.join('\n').repeat(6);
    const started = performance.now();
    const marks = keywords(long + '。', 7);
    expect(performance.now() - started).toBeLessThan(1500);
    wellFormedMarks(long + '。', marks);
  });
  it('works without Intl.Segmenter (structured marks only)', async () => {
    const original = Object.getOwnPropertyDescriptor(Intl, 'Segmenter')!;
    Object.defineProperty(Intl, 'Segmenter', { value: undefined, configurable: true, writable: true });
    try {
      vi.resetModules();
      const fresh = await import('./transcript');
      const text = '明天下午三点在三楼会议室开项目例会，王经理负责准备预算表，预算控制在5万元以内。';
      const marks = fresh.keywords(text, 3);
      expect(marks.map((m) => text.slice(m.start, m.end))).toContain('明天下午三点');
      wellFormedMarks(text, marks);
    } finally {
      Object.defineProperty(Intl, 'Segmenter', original);
      vi.resetModules();
    }
  });
});

describe('tag suggestion', () => {
  it.each([
    ['明天下午三点在三楼会议室开项目例会，王经理负责准备预算表。', '会议'],
    ['会议纪要：下周二前完成原型，张伟负责', '会议'],
    ['记得给妈妈打电话，顺便问一下周六几点到家。', '待办'],
    ['周五前整理对照组数据，记录完成时间与错误率。', '待办'],
    ['让可见性与语音可懂度分开测量。\n可能存在交互效应，样本 n=120，不直接认定为创新。', '论文'],
    ['升级到 Python 3.12 之后 numpy 报错了，先回滚到 v1.4.2 再看。', '技术'],
    ['突然有个想法，如果能把录音笔做成一个可以挂在胸前的小圆片，开会的时候就不用拿在手里了。', '灵感'],
    ['下个月十号去上海出差，住在浦东，酒店预算一晚六百以内，记得提前订高铁票。', '出行'],
    ['这个月要报销的发票一共三张，预算还剩两千。', '财务'],
  ])('%s → #%s', (text, tag) => expect(suggestTag(text, [])).toBe(tag));
  it('stays quiet without enough evidence', () => {
    for (const text of ['明天', '引用一下他说的话', '今天天气还挺好的', '长按 ⑦ 在三种模式之间循环。', '飞鼠按住时，翻页键临时变成左右键。', ''])
      expect(suggestTag(text, [])).toBeNull();
  });
  it('never suggests a tag the note already has', () => {
    const text = '记得给妈妈打电话，顺便问一下周六几点到家。';
    expect(suggestTag(text, ['待办'])).toBeNull();
    expect(suggestTag('明天下午三点在三楼会议室开项目例会，王经理负责准备预算表。', ['会议记录'])).not.toBe('会议');
  });
});
