// Icons + Verbatim logomark
const Ico = {
  inbox: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M2 9h3l1.2 2h3.6L11 9h3M2 9l2-5h8l2 5M2 9v3.5A1.5 1.5 0 0 0 3.5 14h9a1.5 1.5 0 0 0 1.5-1.5V9"/></svg>,
  user: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="6" r="2.5"/><path d="M3 13.5c0-2.5 2.2-4 5-4s5 1.5 5 4"/></svg>,
  commit: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 4l3 3-3 3M7 10h6"/></svg>,
  decision: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v12M3 5l5-3 5 3M3 11l5 3 5-3"/></svg>,
  question: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6"/><path d="M6.5 6.5c0-1 .7-2 1.6-2s1.6.8 1.6 1.8c0 1.4-1.6 1.6-1.6 2.5M8 11.4v.2"/></svg>,
  blocker: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6"/><path d="M4 4l8 8"/></svg>,
  slack: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"><rect x="2" y="6" width="3" height="2" rx="1"/><rect x="6" y="2" width="2" height="3" rx="1"/><rect x="11" y="8" width="3" height="2" rx="1"/><rect x="8" y="11" width="2" height="3" rx="1"/><rect x="6" y="6" width="4" height="4" rx="1"/></svg>,
  meeting: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="9" height="8" rx="1.5"/><path d="M11 7l3-2v6l-3-2z"/></svg>,
  pr: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="4" cy="4" r="1.6"/><circle cx="4" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><path d="M4 5.6v4.8M10.4 12H8a4 4 0 0 1-4-4V5.6M10 9.6 12 12l-2 2.4"/></svg>,
  github: (p) => <svg {...p} viewBox="0 0 16 16" fill="currentColor"><path d="M8 .2a8 8 0 0 0-2.5 15.6c.4.1.6-.2.6-.4v-1.6c-2.2.5-2.7-1-2.7-1-.4-.9-.9-1.2-.9-1.2-.7-.5.1-.5.1-.5.8 0 1.2.8 1.2.8.7 1.2 1.9.9 2.4.7.1-.5.3-.9.5-1.1-1.8-.2-3.6-.9-3.6-3.9 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8a7.5 7.5 0 0 1 4 0c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.6.8 1.3.8 2.1 0 3-1.8 3.7-3.6 3.9.3.2.5.7.5 1.4v2c0 .2.2.5.6.4A8 8 0 0 0 8 .2Z"/></svg>,
  search: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>,
  plus: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 3v10M3 8h10"/></svg>,
  chevron: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M6 4l4 4-4 4"/></svg>,
  chevronD: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6l4 4 4-4"/></svg>,
  filter: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M2 4h12M4 8h8M6 12h4"/></svg>,
  sort: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M4 3v10M2 11l2 2 2-2M9 5h5M9 9h4M9 13h2"/></svg>,
  arrow: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 8h10M9 4l4 4-4 4"/></svg>,
  link: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M7 9l2-2M6.5 4.5l1-1a2.8 2.8 0 0 1 4 4l-1 1M9.5 11.5l-1 1a2.8 2.8 0 0 1-4-4l1-1"/></svg>,
  copy: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"/><path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5"/></svg>,
  check: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M3 8.5 6.5 12 13 4.5"/></svg>,
  more: (p) => <svg {...p} viewBox="0 0 16 16" fill="currentColor"><circle cx="4" cy="8" r="1.2"/><circle cx="8" cy="8" r="1.2"/><circle cx="12" cy="8" r="1.2"/></svg>,
  x: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>,
  sun: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3"/></svg>,
  moon: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M13 9.5A5.5 5.5 0 1 1 6.5 3a4.5 4.5 0 0 0 6.5 6.5Z"/></svg>,
  bolt: (p) => <svg {...p} viewBox="0 0 16 16" fill="currentColor"><path d="M9 1 3 9h4l-1 6 6-8H8z"/></svg>,
  shield: (p) => <svg {...p} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 1.5 2.5 4v4c0 3.5 2.5 5.7 5.5 6.5 3-.8 5.5-3 5.5-6.5V4z"/></svg>,
};

// ====== Verbatim logomark ======
// Two pairs of pill marks tilted -12°. Lower-left pair = opening curly quote.
// Upper-right pair = closing curly quote. Empty diagonal between = the captured statement.
function Logomark({ size = 22, color = "currentColor", accent }) {
  const c = accent || color;
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-label="Verbatim">
      <g transform="rotate(-12 16 16)">
        {/* Opening (lower-left) */}
        <rect x="5" y="17" width="3.2" height="9" rx="1.6" fill={c} />
        <rect x="10" y="17" width="3.2" height="9" rx="1.6" fill={c} />
        {/* Closing (upper-right) */}
        <rect x="18.8" y="6" width="3.2" height="9" rx="1.6" fill={c} />
        <rect x="23.8" y="6" width="3.2" height="9" rx="1.6" fill={c} />
      </g>
    </svg>
  );
}

Object.assign(window, { Ico, Logomark });


// ===== DATA =====
// Sample data for Verbatim — engineering team scenarios

const PEOPLE = {
  priya:   { name: "Priya Raman",     handle: "priya",     initials: "PR", color: "purple" },
  marcus:  { name: "Marcus Holt",     handle: "marcus",    initials: "MH", color: "teal"   },
  sasha:   { name: "Sasha Levin",     handle: "sasha",     initials: "SL", color: "rose"   },
  jules:   { name: "Jules Okafor",    handle: "jules",     initials: "JO", color: "amber"  },
  wei:     { name: "Wei Chen",        handle: "wei",       initials: "WC", color: "blue"   },
  dani:    { name: "Dani Park",       handle: "dani",      initials: "DP", color: "slate"  },
  ren:     { name: "Ren Takahashi",   handle: "ren",       initials: "RT", color: "purple" },
  amal:    { name: "Amal Singh",      handle: "amal",      initials: "AS", color: "teal"   },
};

const ITEMS = [
  {
    id: "VRB-417",
    type: "commitment",
    summary: "Ship the events-store migration to platform staging by Friday",
    quote: "yeah I can have the migration ready by EOW — running the dual-write tonight, cutover Thursday",
    owner: "priya",
    due: "Fri",
    dueState: "soon",
    status: "open",
    confirmed: true,
    source: { kind: "slack", channel: "platform-eng", ts: "Mon 4:17pm" },
    confidence: 0.94,
    unread: true,
  },
  {
    id: "VRB-416",
    type: "decision",
    summary: "Adopt Postgres logical replication over Debezium for the events store",
    quote: "let's go with native logical replication — Debezium is overkill for our volume and adds an operational surface we don't want",
    owner: "marcus",
    due: null,
    status: "ratified",
    confirmed: true,
    source: { kind: "meeting", channel: "Platform Arch Sync", ts: "Mon 11:00am" },
    confidence: 0.91,
    unread: true,
  },
  {
    id: "VRB-415",
    type: "blocker",
    summary: "Rate limiter rollout blocked on infra capacity for Redis Enterprise",
    quote: "we're stuck — finance hasn't approved the Redis Enterprise SKU and the budget window closes Thursday",
    owner: "sasha",
    due: "Thu",
    dueState: "overdue",
    status: "open",
    confirmed: false,
    source: { kind: "slack", channel: "infra-standup", ts: "today 9:42am" },
    confidence: 0.88,
    unread: true,
  },
  {
    id: "VRB-414",
    type: "question",
    summary: "Should we deprecate the v1 webhooks API in Q3, or keep parity through Q4?",
    quote: "honestly I don't know — what's the smallest customer still on v1? if it's an enterprise account we can't just sunset",
    owner: "jules",
    due: null,
    status: "open",
    confirmed: false,
    source: { kind: "pr", channel: "verbatim/api#2841", ts: "today 8:15am" },
    confidence: 0.79,
  },
  {
    id: "VRB-413",
    type: "commitment",
    summary: "Wei to write up the incident retrospective for the Apr 30 auth outage",
    quote: "I'll have the retro doc up by Wednesday EOD with a draft of the action items",
    owner: "wei",
    due: "Wed",
    dueState: "soon",
    status: "open",
    confirmed: true,
    source: { kind: "slack", channel: "incidents", ts: "yesterday 6:02pm" },
    confidence: 0.97,
  },
  {
    id: "VRB-412",
    type: "decision",
    summary: "Hold all schema changes to billing tables until after the Q2 close",
    quote: "no schema changes to billing until after Q2 close — anyone touching that table needs an exception from me",
    owner: "dani",
    due: null,
    status: "ratified",
    confirmed: true,
    source: { kind: "slack", channel: "eng-leads", ts: "yesterday 2:30pm" },
    confidence: 0.93,
  },
  {
    id: "VRB-411",
    type: "blocker",
    summary: "Mobile build pipeline failing on the new M3 fleet — can't ship 4.2.0",
    quote: "the M3 runners are dropping the codesign step intermittently, we've been stuck on it since Friday",
    owner: "ren",
    due: null,
    status: "open",
    confirmed: true,
    source: { kind: "slack", channel: "mobile-eng", ts: "yesterday 11:14am" },
    confidence: 0.86,
  },
  {
    id: "VRB-410",
    type: "question",
    summary: "Do we have to support the legacy webhook payload shape for the Acme migration?",
    quote: "Acme is asking — do we have to keep the legacy payload shape post-migration or can we move them to v2 fields?",
    owner: "amal",
    due: null,
    status: "open",
    confirmed: false,
    source: { kind: "slack", channel: "cust-acme", ts: "yesterday 10:48am" },
    confidence: 0.74,
  },
  {
    id: "VRB-409",
    type: "commitment",
    summary: "Add tracing spans to the ingest worker before next week's load test",
    quote: "I'll get the spans in before Tuesday — won't be exhaustive but enough to find the hotspot",
    owner: "marcus",
    due: "Tue",
    dueState: "soon",
    status: "open",
    confirmed: true,
    source: { kind: "pr", channel: "verbatim/core#1903", ts: "2d ago" },
    confidence: 0.92,
  },
  {
    id: "VRB-408",
    type: "decision",
    summary: "Move the public docs from Mintlify to a self-hosted MDX setup",
    quote: "we're moving off Mintlify — owning the docs stack is worth the cost given the customizations we keep needing",
    owner: "jules",
    due: null,
    status: "ratified",
    confirmed: true,
    source: { kind: "meeting", channel: "DevEx Weekly", ts: "2d ago" },
    confidence: 0.89,
  },
];

// Source thread for the selected item (VRB-417: Priya's migration commitment)
const SLACK_THREAD = {
  channel: "platform-eng",
  members: 47,
  messages: [
    {
      who: "marcus",
      ts: "4:11 PM",
      body: <>hey <span className="mention">@priya</span>, where are we on the events-store cutover? infra is asking when they can decom the old shard set.</>,
    },
    {
      who: "marcus",
      ts: "4:12 PM",
      same: true,
      body: <>they want a date they can put on the calendar — even a soft target</>,
    },
    {
      who: "priya",
      ts: "4:15 PM",
      body: <>almost there — dual-write has been clean for ~3 days, p99 lag is under 400ms</>,
    },
    {
      who: "priya",
      ts: "4:17 PM",
      same: true,
      highlight: true,
      body: <>yeah I can have the migration ready by EOW — running the dual-write tonight, cutover Thursday</>,
      reactions: [
        { emoji: "🎯", count: 3 },
        { emoji: "🙏", count: 2, mine: true },
        { emoji: "verbatim", count: 1, custom: true },
      ],
    },
    {
      who: "marcus",
      ts: "4:19 PM",
      body: <>perfect. I'll tell infra Thursday-EOD as the cutover window. Anything you need from me?</>,
    },
    {
      who: "priya",
      ts: "4:21 PM",
      body: <>just a heads-up on PR review turnaround — I'll have <code>core#1908</code> up tonight</>,
    },
  ],
};

const RELATED = [
  {
    quote: "dual-write has been clean for ~3 days, p99 lag is under 400ms",
    who: "Priya · same thread · 4:15 PM",
    id: "VRB-417b",
  },
  {
    quote: "I'll tell infra Thursday-EOD as the cutover window",
    who: "Marcus · same thread · 4:19 PM",
    id: "VRB-418",
  },
  {
    quote: "let's go with native logical replication — Debezium is overkill",
    who: "Marcus · Platform Arch Sync · Mon 11am",
    id: "VRB-416",
  },
];

Object.assign(window, { PEOPLE, ITEMS, SLACK_THREAD, RELATED });


// ===== APP =====
// Verbatim — main app
const { useState, useMemo } = React;

const TYPE_META = {
  commitment: { label: "Commitment", icon: Ico.commit, plural: "Commitments" },
  decision:   { label: "Decision",   icon: Ico.decision, plural: "Decisions" },
  question:   { label: "Question",   icon: Ico.question, plural: "Questions" },
  blocker:    { label: "Blocker",    icon: Ico.blocker, plural: "Blockers" },
};

const SOURCE_ICONS = {
  slack: Ico.slack,
  meeting: Ico.meeting,
  pr: Ico.github,
};

const SOURCE_LABEL = {
  slack: "Slack",
  meeting: "Meeting",
  pr: "Pull request",
};

function Avatar({ p, size }) {
  if (!p) return null;
  const style = size ? { width: size, height: size, fontSize: size * 0.42 } : null;
  return <span className={`avatar ${p.color}`} style={style} title={p.name}>{p.initials}</span>;
}

function Sidebar({ items, active, onPick }) {
  const counts = useMemo(() => {
    const c = { inbox: 0, mine: 0 };
    Object.keys(TYPE_META).forEach(k => c[k] = 0);
    items.forEach(it => {
      if (it.unread) c.inbox++;
      if (it.owner === "priya") c.mine++;
      c[it.type] = (c[it.type] || 0) + 1;
    });
    return c;
  }, [items]);

  const NavItem = ({ id, icon: I, label, count, hasDot, dotColor }) => (
    <div
      className={`nav-item ${active === id ? 'active' : ''} ${hasDot ? 'has-dot' : ''}`}
      onClick={() => onPick(id)}
    >
      {dotColor
        ? <span className="ico" style={{ background: dotColor }} />
        : <I className="ico" />
      }
      <span>{label}</span>
      {count != null && <span className="count">{count}</span>}
    </div>
  );

  return (
    <aside className="sidebar">
      <div className="brand">
        <Logomark size={22} accent="var(--accent)" />
        <span className="brand-word">verbatim</span>
        <span className="brand-team">engineering</span>
        <Ico.chevronD className="brand-chevron" style={{ width: 11, height: 11 }} />
      </div>

      <div className="search-row">
        <button className="search-btn">
          <Ico.search style={{ width: 12, height: 12 }} />
          <span>Search or jump to…</span>
          <span className="kbd">⌘K</span>
        </button>
        <button className="icon-btn" title="New">
          <Ico.plus style={{ width: 12, height: 12 }} />
        </button>
      </div>

      <nav className="nav">
        <NavItem id="inbox" icon={Ico.inbox} label="Inbox" count={counts.inbox} hasDot />
        <NavItem id="mine" icon={Ico.user} label="My items" count={counts.mine} />

        <div className="nav-section">
          <span>Workspace</span>
          <Ico.plus className="plus" style={{ width: 11, height: 11 }} />
        </div>
        <NavItem id="commitment" icon={TYPE_META.commitment.icon} label="Commitments" count={counts.commitment} />
        <NavItem id="decision"   icon={TYPE_META.decision.icon}   label="Decisions"   count={counts.decision} />
        <NavItem id="question"   icon={TYPE_META.question.icon}   label="Questions"   count={counts.question} />
        <NavItem id="blocker"    icon={TYPE_META.blocker.icon}    label="Blockers"    count={counts.blocker} />

        <div className="nav-section"><span>Sources</span></div>
        <NavItem id="src-slack"   icon={Ico.slack}    label="Slack"          count={6} />
        <NavItem id="src-meeting" icon={Ico.meeting}  label="Meetings"       count={2} />
        <NavItem id="src-pr"      icon={Ico.github}   label="Pull requests"  count={2} />

        <div className="nav-section"><span>Teams</span><Ico.plus className="plus" style={{ width: 11, height: 11 }}/></div>
        <div className="nav-item team"><span className="ico" style={{ background: "#a78bfa" }} /><span>Platform</span><span className="count">12</span></div>
        <div className="nav-item team"><span className="ico" style={{ background: "#5eead4" }} /><span>DevEx</span><span className="count">7</span></div>
        <div className="nav-item team"><span className="ico" style={{ background: "#fb7185" }} /><span>Infra</span><span className="count">9</span></div>
        <div className="nav-item team"><span className="ico" style={{ background: "#fbbf24" }} /><span>Mobile</span><span className="count">4</span></div>

        <div className="nav-section"><span>Integrations</span></div>
        <NavItem id="int-mcp" icon={Ico.bolt} label="MCP for agents" />
        <NavItem id="int-cli" icon={Ico.arrow} label="CLI" />
      </nav>

      <div className="sidebar-footer">
        <div className="ingest-pulse" />
        <span className="ingest-label">Ingesting · <span className="ingest-count">2,847</span> today</span>
      </div>
    </aside>
  );
}

function FilterTab({ id, label, count, active, onPick, dotClass }) {
  return (
    <div
      className={`filter-tab ${active ? 'active' : ''}`}
      onClick={() => onPick(id)}
    >
      {dotClass && <span className={`dot type-dot ${dotClass}`} />}
      <span>{label}</span>
      <span className="ct">{count}</span>
    </div>
  );
}

function ListPane({ items, filter, setFilter, selectedId, onSelect }) {
  const filtered = useMemo(() => {
    if (filter === "all") return items;
    return items.filter(i => i.type === filter);
  }, [items, filter]);

  const counts = useMemo(() => {
    const c = { all: items.length };
    Object.keys(TYPE_META).forEach(k => c[k] = items.filter(i => i.type === k).length);
    return c;
  }, [items]);

  return (
    <section className="list-pane">
      <div className="list-header">
        <div className="list-header-top">
          <div className="list-title">Inbox</div>
          <div className="list-meta">{filtered.length} items · last sync 12s ago</div>
        </div>
        <div className="filter-tabs">
          <FilterTab id="all" label="All" count={counts.all} active={filter==='all'} onPick={setFilter} />
          <FilterTab id="commitment" label="Commitments" count={counts.commitment} active={filter==='commitment'} onPick={setFilter} dotClass="commitment" />
          <FilterTab id="decision"   label="Decisions"   count={counts.decision}   active={filter==='decision'}   onPick={setFilter} dotClass="decision" />
          <FilterTab id="question"   label="Questions"   count={counts.question}   active={filter==='question'}   onPick={setFilter} dotClass="question" />
          <FilterTab id="blocker"    label="Blockers"    count={counts.blocker}    active={filter==='blocker'}    onPick={setFilter} dotClass="blocker" />
        </div>
      </div>
      <div className="toolbar">
        <div className="toolbar-chip set">
          <Ico.filter style={{ width: 11, height: 11 }} />
          <span>Status</span>
          <strong>open</strong>
        </div>
        <div className="toolbar-chip">
          <Ico.plus style={{ width: 11, height: 11 }} /> Owner
        </div>
        <div className="toolbar-chip">
          <Ico.plus style={{ width: 11, height: 11 }} /> Source
        </div>
        <div className="toolbar-chip">
          <Ico.plus style={{ width: 11, height: 11 }} /> Confidence
        </div>
        <div className="toolbar-spacer" />
        <div className="toolbar-sort">
          <Ico.sort style={{ width: 12, height: 12 }} />
          <span>Newest</span>
          <Ico.chevronD style={{ width: 10, height: 10 }} />
        </div>
      </div>
      <div className="list-scroll">
        {filtered.map(it => (
          <ListRow key={it.id} item={it} selected={it.id === selectedId} onClick={() => onSelect(it.id)} />
        ))}
      </div>
    </section>
  );
}

function ListRow({ item, selected, onClick }) {
  const owner = PEOPLE[item.owner];
  const SrcIco = SOURCE_ICONS[item.source.kind];
  return (
    <div
      className={`row ${selected ? 'selected' : ''} ${item.unread ? 'unread' : ''}`}
      onClick={onClick}
    >
      <div className="row-type">
        <span className={`type-dot ${item.type}`} />
        <span className="row-id">{item.id}</span>
      </div>
      <div className="row-summary">{item.summary}</div>
      <div className="row-meta">
        <Avatar p={owner} />
        {item.due
          ? <span className={`due ${item.dueState || ''}`}>{item.due}</span>
          : <span style={{ color: "var(--text-4)" }}>—</span>
        }
      </div>
      <div className="row-quote">
        <q>{item.quote}</q>
        <span className="attr">{owner.name.split(" ")[0]} · {item.source.channel}</span>
      </div>
      <div className="row-foot">
        <span className="src">
          <SrcIco />
          <span>{SOURCE_LABEL[item.source.kind]}</span>
        </span>
        <span className="dot" />
        <span>{item.source.channel}</span>
        <span className="dot" />
        <span>{item.source.ts}</span>
        <span className="dot" />
        <span className={`status ${item.confirmed ? 'confirmed' : 'open'}`}>
          {item.confirmed ? '✓ confirmed' : 'unconfirmed'}
        </span>
      </div>
    </div>
  );
}

function DetailPane({ item }) {
  if (!item) return <section className="detail" />;
  const owner = PEOPLE[item.owner];
  const SrcIco = SOURCE_ICONS[item.source.kind];
  const Tmeta = TYPE_META[item.type];

  return (
    <section className="detail">
      <div className="detail-header">
        <div className="breadcrumb">
          <span>Inbox</span>
          <span className="sep">/</span>
          <span>{Tmeta.plural}</span>
          <span className="sep">/</span>
          <span className="id">{item.id}</span>
        </div>
        <div className="header-actions">
          <button className="hdr-btn">
            <Ico.link style={{ width: 12, height: 12 }} /> Copy link
          </button>
          <button className="hdr-btn">
            <Ico.user style={{ width: 12, height: 12 }} /> Reassign
          </button>
          <button className="hdr-btn primary">
            <Ico.check style={{ width: 12, height: 12 }} /> Acknowledge
            <span className="kbd">E</span>
          </button>
          <button className="hdr-btn" style={{ padding: "5px 6px" }}>
            <Ico.more style={{ width: 14, height: 14 }} />
          </button>
        </div>
      </div>

      <div className="detail-body">
        <div className="detail-main">
          <div className="entity-eyebrow">
            <span className={`type-dot ${item.type}`} />
            <span>{Tmeta.label}</span>
            <span style={{ color: "var(--text-4)" }}>·</span>
            <span style={{ fontFamily: '"JetBrains Mono", monospace', textTransform: "none", letterSpacing: 0 }}>{item.id}</span>
          </div>

          <h1 className="entity-title">{item.summary}</h1>

          {/* ===== THE QUOTE — HERO ===== */}
          <div className="quote-hero">
            <div className="quote-hero-label">
              <span className="lock">verbatim</span>
              <span>exact words from source · evidence locked</span>
            </div>
            <div className="quote-hero-text">
              <q>{item.quote}</q>
            </div>
            <div className="quote-hero-attr">
              <Avatar p={owner} size={16} />
              <strong>{owner.name}</strong>
              <span className="sep">·</span>
              <span>#{item.source.channel}</span>
              <span className="sep">·</span>
              <span>{item.source.ts}</span>
              <span className="sep">·</span>
              <button className="hdr-btn" style={{ padding: "2px 6px", fontSize: 10.5 }}>
                <Ico.arrow style={{ width: 10, height: 10 }} /> Jump to source
              </button>
            </div>
          </div>

          {/* ===== Source context (Slack thread) ===== */}
          <h3 className="section-h">
            <span>Source context</span>
            <span className="src-chip">
              <Ico.slack style={{ width: 11, height: 11 }} />
              <span>#{item.source.channel}</span>
            </span>
            <span className="line" />
            <span style={{ textTransform: "none", letterSpacing: 0, color: "var(--text-4)", fontWeight: 400, fontSize: 11 }}>
              extracted in 1.8s · {Math.round(item.confidence * 100)}% confidence
            </span>
          </h3>

          <div className="slack-frame">
            <div className="slack-head">
              <Ico.slack style={{ width: 13, height: 13, color: "var(--text-3)" }} />
              <span className="ch">{SLACK_THREAD.channel}</span>
              <span style={{ color: "var(--text-4)" }}>·</span>
              <span style={{ color: "var(--text-3)" }}>Mon, May 18 · thread</span>
              <span className="members">{SLACK_THREAD.members} members</span>
            </div>

            {SLACK_THREAD.messages.map((m, i) => {
              const p = PEOPLE[m.who];
              const next = SLACK_THREAD.messages[i + 1];
              return (
                <React.Fragment key={i}>
                  <div className={`slack-msg ${m.same ? 'same' : ''} ${m.highlight ? 'highlight' : ''}`}>
                    <div className="av-slot"><Avatar p={p} /></div>
                    <div>
                      {!m.same && (
                        <div className="who">
                          <strong>{p.name}</strong>
                          <span className="ts">{m.ts}</span>
                        </div>
                      )}
                      <div className="body">{m.body}</div>
                      {m.reactions && (
                        <div className="reactions">
                          {m.reactions.map((r, ri) => (
                            <span key={ri} className={`reaction ${r.mine ? 'mine' : ''}`}>
                              {r.custom
                                ? <Logomark size={10} accent="currentColor" />
                                : <span>{r.emoji}</span>}
                              <span>{r.count}</span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                  {m.highlight && (
                    <div className="verbatim-trace">
                      <Logomark size={13} accent="var(--accent)" className="mini-mark" />
                      <span>
                        <strong>verbatim</strong> extracted <strong>commitment</strong> from this line
                        <span className="arrow"> →</span> <strong>{item.id}</strong>
                      </span>
                      <span style={{ marginLeft: "auto", color: "var(--text-4)" }}>
                        confidence {Math.round(item.confidence * 100)}%
                      </span>
                    </div>
                  )}
                </React.Fragment>
              );
            })}

            {/* Bot reply in-thread */}
            <BotReply item={item} owner={owner} />
          </div>
        </div>

        {/* ===== Right rail ===== */}
        <aside className="detail-side">
          <div className="side-block">
            <h4 className="side-h">Properties</h4>
            <div className="side-row">
              <span className="k">Type</span>
              <span className="v">
                <span className={`type-dot ${item.type}`} style={{ width: 7, height: 7 }} />
                {Tmeta.label}
              </span>
            </div>
            <div className="side-row">
              <span className="k">Owner</span>
              <span className="v"><Avatar p={owner} size={16} /> {owner.name}</span>
            </div>
            <div className="side-row">
              <span className="k">Status</span>
              <span className="v">
                <span className="type-dot" style={{ background: "#5eead4", width: 7, height: 7 }} /> Open
              </span>
            </div>
            <div className="side-row">
              <span className="k">Due</span>
              <span className="v">{item.due ? `${item.due}, May 22` : <span className="v muted">—</span>}</span>
            </div>
            <div className="side-row">
              <span className="k">Team</span>
              <span className="v"><span className="type-dot" style={{ background: "#a78bfa", width: 7, height: 7 }}/> Platform</span>
            </div>
            <div className="side-row">
              <span className="k">Extracted</span>
              <span className="v muted">Mon 4:17 PM</span>
            </div>
          </div>

          <div className="side-block">
            <h4 className="side-h">Evidence</h4>
            <div className="side-row">
              <span className="k">Confidence</span>
              <span className="v" style={{ gap: 8 }}>
                <span className="confidence-bar"><div style={{ width: `${item.confidence * 100}%` }}/></span>
                <span style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round(item.confidence * 100)}%</span>
              </span>
            </div>
            <div className="side-row">
              <span className="k">Source</span>
              <span className="v"><SrcIco style={{ width: 11, height: 11, color: "var(--text-3)" }} /> #{item.source.channel}</span>
            </div>
            <div className="side-row">
              <span className="k">Confirmed by</span>
              <span className="v">
                <Avatar p={PEOPLE.priya} size={14} />
                <span style={{ fontSize: 11.5 }}>reacted 🎯</span>
              </span>
            </div>
            <div className="side-row">
              <span className="k">Model</span>
              <span className="v muted" style={{ fontFamily: '"JetBrains Mono", monospace', fontSize: 11 }}>haiku-4.5</span>
            </div>
          </div>

          <div className="side-block">
            <h4 className="side-h">Linked</h4>
            <div className="linked-item">
              <Ico.github style={{ width: 12, height: 12, color: "var(--text-3)" }} />
              <span className="ltext">verbatim/core#1908</span>
              <span className="id">PR</span>
            </div>
            <div className="linked-item">
              <Ico.decision style={{ width: 12, height: 12, color: "var(--text-3)" }} />
              <span className="ltext">VRB-416 logical replication</span>
              <span className="id">→</span>
            </div>
            <div className="linked-item">
              <Ico.meeting style={{ width: 12, height: 12, color: "var(--text-3)" }} />
              <span className="ltext">Platform Arch Sync · Mon</span>
            </div>
          </div>

          <div className="side-block">
            <h4 className="side-h">Related verbatim</h4>
            {RELATED.map((r, i) => (
              <div key={i} className="related-quote">
                <q style={{ quotes: '"\u201C" "\u201D"' }}>{r.quote}</q>
                <span className="ra">{r.who}</span>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </section>
  );
}

function BotReply({ item, owner }) {
  return (
    <div className="bot-reply">
      <div className="bot-reply-head">
        <div className="bot-av">
          <Logomark size={13} accent="#fff" />
        </div>
        <strong>verbatim</strong>
        <span className="app-tag">App</span>
        <span className="ts">4:17 PM · only visible to you</span>
      </div>
      <div className="bot-reply-body">
        I locked a <strong style={{ color: "var(--accent)" }}>commitment</strong> from Priya's last message. Quote preserved as evidence — nothing paraphrased.
        <div className="extraction-card">
          <div className="ec-row">
            <span className="ec-label">Type</span>
            <span className="ec-val">
              <span className="type-dot commitment" style={{ width: 7, height: 7 }} /> Commitment
              <span style={{ color: "var(--text-4)", fontFamily: '"JetBrains Mono", monospace', fontSize: 11 }}>VRB-417</span>
            </span>
          </div>
          <div className="ec-row">
            <span className="ec-label">Owner</span>
            <span className="ec-val">
              <Avatar p={owner} size={16} />
              <span>{owner.name}</span>
            </span>
          </div>
          <div className="ec-row">
            <span className="ec-label">Due</span>
            <span className="ec-val">Friday, May 22 <span style={{ color: "var(--text-4)" }}>· inferred from "EOW"</span></span>
          </div>
          <div className="ec-row">
            <span className="ec-label">Quote</span>
            <span className="ec-val">
              <span className="ec-quote">"yeah I can have the migration ready by EOW — running the dual-write tonight, cutover Thursday"</span>
            </span>
          </div>
          <div className="ec-actions">
            <button className="ec-btn primary">
              <span style={{ marginRight: 4 }}>👍</span>Confirm
            </button>
            <button className="ec-btn">Edit details</button>
            <button className="ec-btn">Reassign</button>
            <button className="ec-btn">Not a commitment</button>
            <span style={{ marginLeft: "auto", color: "var(--text-4)", fontSize: 11, alignSelf: "center" }}>
              react with 🎯 to confirm
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function TweaksPanel() {
  const [theme, setTheme] = useState(
    document.documentElement.getAttribute('data-theme') || 'dark'
  );
  const change = (t) => {
    setTheme(t);
    window.setTheme(t);
  };
  const dismiss = () => {
    document.getElementById('tweaks-panel').classList.remove('open');
    try { window.parent.postMessage({ type: '__edit_mode_dismissed' }, '*'); } catch(e) {}
  };
  return (
    <div className="tweaks" id="tweaks-panel">
      <span className="tweaks-title">Tweaks</span>
      <div className="seg">
        <button
          data-tweak-theme="dark"
          className={theme === 'dark' ? 'active' : ''}
          onClick={() => change('dark')}
        >
          <Ico.moon /> Dark
        </button>
        <button
          data-tweak-theme="light"
          className={theme === 'light' ? 'active' : ''}
          onClick={() => change('light')}
        >
          <Ico.sun /> Light
        </button>
      </div>
      <button className="tweaks-x" onClick={dismiss}><Ico.x style={{ width: 12, height: 12 }} /></button>
    </div>
  );
}

function App() {
  const [filter, setFilter] = useState("all");
  const [selectedId, setSelectedId] = useState("VRB-417");
  const selected = ITEMS.find(i => i.id === selectedId);

  return (
    <>
      <div className="shell">
        <Sidebar items={ITEMS} active="inbox" onPick={() => {}} />
        <ListPane
          items={ITEMS}
          filter={filter}
          setFilter={setFilter}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
        <DetailPane item={selected} />
      </div>
      <TweaksPanel />
    </>
  );
}

window.setTheme = (t) => {
  document.documentElement.setAttribute('data-theme', t);
  try { window.parent.postMessage({ type: '__edit_mode_set_keys', edits: { theme: t } }, '*'); } catch(e) {}
};

const root = ReactDOM.createRoot(document.getElementById('app'));
root.render(<App />);
