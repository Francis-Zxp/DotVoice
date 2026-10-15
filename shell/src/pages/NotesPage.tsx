import { AnimatePresence, motion, useIsPresent } from 'motion/react';
import { Check, Copy, Inbox, NotebookPen, Pencil, Plus, Search, SearchX, Sparkles, Trash2, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Empty, PageHeader, spring } from '../components/ui';
import { clock, dayKey, dayLabel, duration, fullDate, shortClock } from '../lib/format';
import { act, useStore } from '../lib/store';
import { runs, suggestTag } from '../lib/transcript';
import type { Note } from '../lib/types';

const INBOX = '__inbox__';

export function NotesPage() {
  const notes = useStore((s) => s.notes);
  const tags = useStore((s) => s.tags);
  const warning = useStore((s) => s.notesWarning);
  const refresh = useStore((s) => s.refreshNotes);
  const notify = useStore((s) => s.notify);
  const [filter, setFilter] = useState<string>('');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => { refresh(); }, [refresh]);
  // Ctrl+F finds, Esc clears the search.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && !e.altKey && e.key.toLowerCase() === 'f') { e.preventDefault(); search.current?.focus(); search.current?.select(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => { if (warning) notify(warning, 'error'); }, [warning, notify]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return notes
      .filter((n) => !filter || (filter === INBOX ? n.tags.length === 0 : n.tags.includes(filter)))
      .filter((n) => !q || n.text.toLowerCase().includes(q) || n.source.toLowerCase().includes(q))
      .sort((a, b) => b.created - a.created);
  }, [notes, filter, query]);

  const groups = useMemo(() => {
    const result: { key: string; label: string; items: Note[] }[] = [];
    for (const note of visible) {
      const key = dayKey(note.created);
      const group = result.find((g) => g.key === key);
      if (group) group.items.push(note);
      else result.push({ key, label: dayLabel(note.created), items: [note] });
    }
    return result;
  }, [visible]);

  const current = visible.find((n) => n.id === selectedId) ?? visible[0] ?? null;
  const inboxCount = notes.filter((n) => n.tags.length === 0).length;

  async function remove(note: Note) {
    if (!(await act('delete_note', { identifier: note.id }))) return;
    await refresh();
    notify('已删除这条笔记', 'info', {
      label: '撤销',
      run: async () => {
        if (await act('restore_note', { identifier: note.id })) {
          await refresh();
          setSelectedId(note.id);
        }
      },
    });
  }

  const filters: { id: string; label: string; count: number }[] = [
    { id: '', label: '全部', count: notes.length },
    { id: INBOX, label: '收件箱', count: inboxCount },
    ...tags.map((t) => ({ id: t, label: t, count: notes.filter((n) => n.tags.includes(t)).length })),
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader eyebrow="先留住，再慢慢想" title="笔记" meta={notes.length ? `${notes.length} 条` : undefined}>
        <label className="search">
          <Search size={15} className="search-ico" />
          <input ref={search} className="field pl-9 pr-9" placeholder="搜索笔记" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索笔记"
            onKeyDown={(e) => { if (e.key === 'Escape') { setQuery(''); e.currentTarget.blur(); } }} />
          <AnimatePresence initial={false}>
            {query ? (
              <motion.button key="clear" type="button" className="search-clear" aria-label="清除搜索" onClick={() => { setQuery(''); search.current?.focus(); }}
                initial={{ opacity: 0, scale: .6 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .6 }} transition={spring.snappy}><X size={12} /></motion.button>
            ) : <motion.kbd key="hint" className="kbd search-hint" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>Ctrl F</motion.kbd>}
          </AnimatePresence>
        </label>
      </PageHeader>
      <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="按标签筛选">
        {filters.map((f) => {
          const on = filter === f.id;
          return (
            <button key={f.id || 'all'} aria-pressed={on} onClick={() => setFilter(f.id)} className="filter-chip">
              {on && <motion.i layoutId="filter-thumb" className="filter-thumb" transition={spring.snappy} />}
              <span className="relative inline-flex items-center gap-1.5">{f.id === INBOX && <Inbox size={12} />}{f.label}<span className="num opacity-55">{f.count}</span></span>
            </button>
          );
        })}
      </div>
      <div className="grid min-h-0 flex-1 gap-4" style={{ gridTemplateColumns: 'minmax(250px, 330px) minmax(0, 1fr)' }}>
        <section className="glass flex min-h-0 flex-col overflow-hidden" aria-label="笔记列表">
          <div className="scroll scroll-even relative z-[1] min-h-0 flex-1 px-1 pb-2">
            {groups.length === 0 && (
              <Empty icon={query || filter ? <SearchX size={20} /> : <NotebookPen size={20} />} title={query || filter ? '没有匹配的笔记' : '还没有笔记'}>
                {query || filter ? '换个关键词，或回到“全部”。' : '切到“笔记”模式，按 ⑥ 说话，说完再按一次就会保存在这里。'}
              </Empty>
            )}
            {groups.map((group) => (
              <div key={group.key}>
                <div className="sticky top-0 z-10 px-2 pb-1 pt-3">
                  <span className="day-chip">{group.label}</span>
                </div>
                <AnimatePresence initial={false}>
                  {group.items.map((note) => {
                    const active = current?.id === note.id;
                    return (
                      <motion.button layout="position" key={note.id} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, x: -16 }}
                        onClick={() => setSelectedId(note.id)} aria-current={active} data-note-id={note.id}
                        className="tile note-item mb-0.5 block w-full px-3 py-2.5">
                        {active && <motion.span layoutId="note-thumb" className="tile-thumb" transition={spring.snappy} />}
                        <span className="relative block">
                          <span className="flex items-center gap-2 text-[11.5px]">
                            <span className="num font-semibold" style={{ color: 'var(--text-2)' }}>{clock(note.created)}</span>
                            {note.duration != null && <span className="faint">{duration(note.duration)}</span>}
                            <span className="ml-auto flex gap-1">{note.tags.slice(0, 2).map((t) => <span key={t} className="faint">#{t}</span>)}</span>
                          </span>
                          <span className="mt-0.5 line-clamp-1 block font-semibold">{note.title}</span>
                          <span className="faint line-clamp-2 block text-[12.5px] leading-5">{note.text}</span>
                        </span>
                      </motion.button>
                    );
                  })}
                </AnimatePresence>
              </div>
            ))}
          </div>
        </section>
        <section className="glass flex min-h-0 flex-col overflow-hidden px-7 py-6">
          <AnimatePresence mode="wait" initial={false}>
            {current ? (
              <DetailPane key={current.id} id={current.id}>
                <NoteDetail note={current} allTags={tags} onDelete={() => remove(current)} onEdit={() => setSelectedId(current.id)} />
              </DetailPane>
            ) : (
              <motion.div key="empty" className="m-auto max-w-[380px] text-center" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <Empty icon={<NotebookPen size={20} />} title="还没说出的想法，也值得被认真记下。">长按 ⑦ 切到笔记模式，按 ⑥ 说话；屏幕上方的便签随声线起伏，说完显示整段文字。</Empty>
              </motion.div>
            )}
          </AnimatePresence>
        </section>
      </div>
    </div>
  );
}

/* With mode="wait" the outgoing note's pane stays mounted through its exit fade, and the next
   note's pane waits for it. The leaving pane is inert, so a click in that moment cannot tag,
   edit or delete the note that is going away. */
function DetailPane({ id, children }: { id: string; children: ReactNode }) {
  const present = useIsPresent();
  return (
    <motion.div className="relative z-[1] flex min-h-0 flex-1 flex-col" data-note-detail={id} inert={!present}
      initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: .08 } }} transition={spring.soft}>
      {children}
    </motion.div>
  );
}

function TagEditor({ note, allTags }: { note: Note; allTags: string[] }) {
  const refresh = useStore((s) => s.refreshNotes);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const suggestion = suggestTag(note.text, note.tags);
  async function save(tags: string[]) {
    if (savingRef.current) return false;
    savingRef.current = true;
    setSaving(true);
    try {
      if (!(await act('set_note_tags', { identifier: note.id, tags }))) return false;
      await refresh();
      return true;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }
  const options = allTags.filter((t) => !note.tags.includes(t));
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {note.tags.length === 0 && <span className="chip"><Inbox size={12} />收件箱</span>}
      <AnimatePresence initial={false}>
        {note.tags.map((t) => (
          <motion.span key={t} layout className="chip pr-1" initial={{ opacity: 0, scale: .8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .8 }} transition={spring.snappy}>#{t}
            <button className="tag-x" disabled={saving} aria-label={`移除标签 ${t}`} onClick={() => save(note.tags.filter((x) => x !== t))}><X size={11} /></button>
          </motion.span>
        ))}
      </AnimatePresence>
      {suggestion && (
        <button className="chip" disabled={saving} style={{ color: 'var(--brand-text)', background: 'var(--brand-soft)' }} onClick={() => save([...note.tags, suggestion])} title="根据内容建议">
          <Sparkles size={12} />建议 #{suggestion}
        </button>
      )}
      {adding ? (
        <form className="flex items-center gap-1" onSubmit={async (e) => {
          e.preventDefault();
          if (draft.trim() && await save([...note.tags, draft.trim()])) { setDraft(''); setAdding(false); }
        }}>
          <input autoFocus disabled={saving} list="tag-options" className="field h-7 w-[120px] text-[12.5px]" value={draft} onChange={(e) => setDraft(e.target.value)}
            onBlur={() => !draft && setAdding(false)} placeholder="标签名" maxLength={12} />
          <datalist id="tag-options">{options.map((t) => <option key={t} value={t} />)}</datalist>
        </form>
      ) : (
        <button className="chip" disabled={saving} onClick={() => setAdding(true)}><Plus size={12} />标签</button>
      )}
    </div>
  );
}

function NoteDetail({ note, allTags, onDelete, onEdit }: { note: Note; allTags: string[]; onDelete: () => void; onEdit: () => void }) {
  const refresh = useStore((s) => s.refreshNotes);
  const notify = useStore((s) => s.notify);
  const [source, setSource] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note.text);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const text = source ? note.source : note.text;

  async function save() {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      if (await act('update_note', { identifier: note.id, text: draft })) {
        await refresh();
        setEditing(false);
        notify('整理稿已保存，服务返回稿保留不变', 'success');
      }
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }
  async function copy() {
    if (await act('copy_text', { text: editing ? draft : text })) notify('已复制', 'success');
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
        <span className="num font-semibold" style={{ color: 'var(--text-2)' }}>{fullDate(note.created)}</span>
        {note.duration != null && <span className="faint">录音 {duration(note.duration)}</span>}
        {note.updated && <span className="faint">· 已于 {shortClock(note.updated)} 修改</span>}
      </div>
      <h2 className="title-lg mt-2" style={{ fontSize: 24 }}>{note.title}</h2>
      <div className="mt-3"><TagEditor note={note} allTags={allTags} /></div>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <div className="segmented" role="group" aria-label="版本">
          {[['clean', '整理稿'], ['source', '服务返回稿']].map(([value, label]) => (
            <button key={value} aria-pressed={(value === 'source') === source} onClick={() => !editing && setSource(value === 'source')}>
              {(value === 'source') === source && <motion.i layoutId="version-thumb" className="thumb" transition={{ type: 'spring', stiffness: 520, damping: 36 }} />}
              <span>{label}</span>
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-1">
          {!editing && <button className="btn btn-ghost btn-sm" disabled={source} onClick={() => { onEdit(); setDraft(note.text); setEditing(true); }}><Pencil size={14} />编辑</button>}
          <button className="btn btn-ghost btn-sm" onClick={copy}><Copy size={14} />复制</button>
          <button className="btn btn-ghost btn-sm btn-danger" disabled={saving} onClick={onDelete} title="删除（可撤销）"><Trash2 size={14} />删除</button>
        </div>
      </div>
      <div className="scroll mt-4 min-h-0 flex-1 pr-2">
        {editing ? (
          <>
            <textarea className="field min-h-[260px] text-[15px]" disabled={saving} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="编辑整理稿" autoFocus />
            <div className="mt-3 flex gap-2">
              <button className="btn btn-primary btn-sm" disabled={saving} onClick={save}><Check size={14} />{saving ? '正在保存…' : '保存修改'}</button>
              <button className="btn btn-ghost btn-sm" disabled={saving} onClick={() => setEditing(false)}><X size={14} />取消</button>
            </div>
          </>
        ) : (
          <div className="note-body" style={{ userSelect: 'text', whiteSpace: 'pre-wrap' }}>
            {runs(text).map((r, i) => r.mark ? <span key={i} className="mark">{r.text}</span> : <span key={i}>{r.text}</span>)}
          </div>
        )}
      </div>
      <p className="faint mt-4 border-t pt-3 text-[12px]" style={{ borderColor: 'var(--hair)' }}>
        服务返回稿原样保存；整理稿去掉独立语气词并分段，可以修改。删除的笔记在回收站保留 30 天。
      </p>
    </>
  );
}
