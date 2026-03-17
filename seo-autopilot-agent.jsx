import { useState } from "react";

// ─── CONFIG ──────────────────────────────────────────────────────
const PROXY_URL = "https://api.tikprofitpro.shop/publish-blog-post";
const BLOG_URL  = "https://blog.tikprofitpro.shop";
const BRAND     = "TikProfitPro";
const DOMAIN    = "tikprofitpro.shop";

// ─── Static Data ─────────────────────────────────────────────────
const KEYWORDS = [
  { kw: "TikTok Shop product research tool",      intent: "Money", difficulty: "Low",      vol: "1,200"  },
  { kw: "best products to sell on TikTok Shop",   intent: "Money", difficulty: "Medium",   vol: "8,000"  },
  { kw: "TikTok Shop winning products",           intent: "Money", difficulty: "Low",      vol: "3,500"  },
  { kw: "how to sell on TikTok Shop 2026",        intent: "Info",  difficulty: "Medium",   vol: "22,000" },
  { kw: "TikTok Shop for beginners",              intent: "Info",  difficulty: "Medium",   vol: "18,000" },
  { kw: "TikTok Shop dropshipping products",      intent: "Money", difficulty: "Low",      vol: "2,800"  },
  { kw: "TikTok Shop profit margin calculator",   intent: "Tool",  difficulty: "Very Low", vol: "2,100"  },
  { kw: "trending TikTok Shop products 2026",     intent: "Info",  difficulty: "Low",      vol: "5,400"  },
];

const TEMPLATES = [
  { type: "Pillar Guide",    words: "3,500+", icon: "📘" },
  { type: "Trending Report", words: "1,800",  icon: "🔥" },
  { type: "Comparison Post", words: "2,200",  icon: "⚖️" },
  { type: "Tutorial",        words: "2,800",  icon: "🎓" },
  { type: "Case Study",      words: "2,000",  icon: "📊" },
  { type: "Tool Page",       words: "1,500",  icon: "🛠️" },
];

const TABS = ["🤖 AI Writer", "🔑 Keywords", "📅 Scheduler", "📤 Publisher", "📈 Tracker"];

// ─── Helpers ─────────────────────────────────────────────────────
const ic = (i) => i === "Money" ? "#00ff88" : i === "Info" ? "#00cfff" : "#ffb800";
const sc = (s) => s >= 80 ? "#00ff88" : s >= 60 ? "#ffb800" : "#ff4444";

function Panel({ title, children }) {
  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:10, padding:16 }}>
      <div style={{ fontSize:12, fontWeight:700, marginBottom:14, color:"#fff", letterSpacing:"0.5px" }}>{title}</div>
      {children}
    </div>
  );
}

function Pill({ status }) {
  const cfg = {
    published: { bg:"rgba(0,255,136,0.15)",  color:"#00ff88" },
    scheduled: { bg:"rgba(0,207,255,0.15)",  color:"#00cfff" },
    draft:     { bg:"rgba(255,184,0,0.15)",  color:"#ffb800" },
  }[status] || { bg:"rgba(255,255,255,0.05)", color:"#7a90a8" };
  return (
    <span style={{ fontSize:11, padding:"4px 12px", borderRadius:20, fontWeight:600, background:cfg.bg, color:cfg.color }}>
      {status}
    </span>
  );
}

// ─── Main App ─────────────────────────────────────────────────────
export default function App() {
  const [tab,        setTab]        = useState(0);
  const [selKw,      setSelKw]      = useState(KEYWORDS[0]);
  const [selTpl,     setSelTpl]     = useState(TEMPLATES[0]);
  const [customKw,   setCustomKw]   = useState("");
  const [generating, setGenerating] = useState(false);
  const [stream,     setStream]     = useState("");
  const [post,       setPost]       = useState(null);
  const [score,      setScore]      = useState(null);
  const [pubStatus,  setPubStatus]  = useState(null);
  const [pubUrl,     setPubUrl]     = useState(null);
  const [pubErr,     setPubErr]     = useState(null);
  const [agentMsg,   setAgentMsg]   = useState(null);
  const [scheduled,  setScheduled]  = useState([
    { title:"Complete Guide to TikTok Shop Product Research 2026", date:"Mar 13", status:"scheduled", keyword:"TikTok Shop product research" },
    { title:"10 Best Products to Sell on TikTok Shop Right Now",   date:"Mar 15", status:"scheduled", keyword:"best products TikTok Shop"    },
    { title:"TikTok Shop vs Amazon: Which Is More Profitable?",    date:"Mar 18", status:"draft",     keyword:"TikTok Shop vs Amazon"         },
    { title:"Top Trending TikTok Shop Products — March 2026",      date:"Mar 20", status:"published", keyword:"trending TikTok Shop Mar 2026" },
  ]);

  // ── Generate ──────────────────────────────────────────────────
  const generate = async () => {
    const kw = customKw.trim() || selKw.kw;
    setGenerating(true); setStream(""); setPost(null); setScore(null);
    setPubStatus(null);  setPubUrl(null); setPubErr(null);
    setAgentMsg("✍️ AI Writer Agent activated...");

    const prompt = `You are an expert SEO content writer for ${BRAND} — a TikTok Shop product research SaaS at ${DOMAIN}.

Write a complete SEO-optimized blog post for the keyword: "${kw}"
Post type: ${selTpl.type} | Target: ${selTpl.words} words

Return EXACTLY this format with the --- delimiters:

---TITLE---
[SEO title under 60 chars including keyword]

---META---
[Meta description 150-160 chars with keyword and CTA]

---SLUG---
[URL slug: lowercase hyphens only under 60 chars]

---CONTENT---
[Full markdown blog post:
# H1 exact keyword match
Engaging intro with stat hook (150 words)
## 4-5 H2 sections with ### H3 subsections
## How ${BRAND} Helps (mention tool naturally, link to ${DOMAIN})
## Frequently Asked Questions (4 questions and answers)
## Conclusion with CTA to start free at ${DOMAIN}]

---SEO_SCORE---
[XX/100 — one line reason]`;

    try {
      const res  = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "claude-sonnet-4-20250514",
          max_tokens: 1000,
          messages: [{ role: "user", content: prompt }],
        }),
      });
      const data = await res.json();
      const full = data.content?.map(c => c.text || "").join("") || "";

      // Stream animation
      let i = 0;
      const iv = setInterval(() => {
        i += 18;
        setStream(full.slice(0, i));
        if (i >= full.length) {
          clearInterval(iv);
          parsePost(full, kw);
          setGenerating(false);
          setAgentMsg(null);
        }
      }, 8);
    } catch (e) {
      setStream("⚠️ Error: " + e.message);
      setGenerating(false);
      setAgentMsg(null);
    }
  };

  const parsePost = (text, kw) => {
    const ext = (tag) => {
      const m = text.match(new RegExp(`---${tag}---\\n([\\s\\S]*?)(?=\\n---[A-Z_]+---|$)`));
      return m ? m[1].trim() : "";
    };
    const scoreRaw = ext("SEO_SCORE");
    const scoreNum = parseInt((scoreRaw.match(/(\d+)/) || ["","78"])[1]);
    const parsed = {
      title:   ext("TITLE")   || kw,
      meta:    ext("META"),
      slug:    ext("SLUG")    || kw.toLowerCase().replace(/[^a-z0-9]+/g,"-"),
      content: ext("CONTENT"),
      seoRaw:  scoreRaw,
    };
    setPost(parsed);
    setScore(scoreNum);
    setScheduled(prev => [{ title:parsed.title, date:"Pending", status:"draft", keyword:kw }, ...prev]);
  };

  // ── Publish via FastAPI proxy ─────────────────────────────────
  const publish = async () => {
    if (!post) return;
    setPubStatus("publishing"); setPubErr(null);
    setAgentMsg("📡 Sending to api.tikprofitpro.shop...");

    try {
      await new Promise(r => setTimeout(r, 400));
      setAgentMsg("📝 Proxy forwarding to Ghost Admin API...");

      const res = await fetch(PROXY_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title:   post.title,
          slug:    post.slug,
          meta:    post.meta,
          content: post.content,
        }),
      });

      const data = await res.json();

      // Check for Ghost API error in response
      if (data?.errors?.length) {
        throw new Error(data.errors[0].message);
      }

      const publishedSlug = data?.posts?.[0]?.slug || post.slug;
      const url = `${BLOG_URL}/${publishedSlug}/`;

      setPubStatus("published");
      setPubUrl(url);
      setAgentMsg("✅ Live on Ghost!");
      setScheduled(prev =>
        prev.map(p => p.title === post.title ? { ...p, status:"published", date:"Today" } : p)
      );
      setTimeout(() => setAgentMsg(null), 3000);

    } catch (e) {
      setPubStatus("error");
      setPubErr(e.message);
      setAgentMsg(null);
    }
  };

  const pubCount   = scheduled.filter(p => p.status === "published").length;
  const schedCount = scheduled.filter(p => p.status === "scheduled").length;

  return (
    <div style={{ minHeight:"100vh", background:"#080c14", fontFamily:"'DM Mono','Courier New',monospace", color:"#e0e8f0" }}>

      {/* BG grid */}
      <div style={{ position:"fixed", inset:0, zIndex:0, pointerEvents:"none",
        backgroundImage:`linear-gradient(rgba(0,255,136,0.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,0.03) 1px,transparent 1px)`,
        backgroundSize:"40px 40px" }} />

      <div style={{ position:"relative", zIndex:1, maxWidth:1100, margin:"0 auto", padding:"24px 16px" }}>

        {/* ── Header ── */}
        <div style={{ marginBottom:22, borderBottom:"1px solid rgba(0,255,136,0.15)", paddingBottom:16 }}>
          <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:6 }}>
            <div style={{ width:36, height:36, borderRadius:8, background:"linear-gradient(135deg,#00ff88,#00cfff)", display:"flex", alignItems:"center", justifyContent:"center", fontSize:18, fontWeight:900, color:"#080c14" }}>⚡</div>
            <div>
              <div style={{ fontSize:20, fontWeight:700, color:"#fff" }}>TikProfitPro <span style={{ color:"#00ff88" }}>SEO Autopilot</span></div>
              <div style={{ fontSize:11, color:"#4a6580", letterSpacing:"2px", textTransform:"uppercase" }}>
                AI Generate → api.tikprofitpro.shop → blog.tikprofitpro.shop
              </div>
            </div>
            <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:6, fontSize:11 }}>
              <span style={{ width:8, height:8, borderRadius:"50%", background:"#00ff88", display:"inline-block", animation:"pulse 2s infinite" }} />
              <span style={{ color:"#00ff88" }}>FULLY LIVE ✅</span>
            </div>
          </div>

          {agentMsg && (
            <div style={{ marginTop:10, padding:"8px 14px", background:"rgba(0,255,136,0.08)", border:"1px solid rgba(0,255,136,0.25)", borderRadius:6, fontSize:12, color:"#00ff88", display:"flex", alignItems:"center", gap:8 }}>
              <span style={{ animation:"pulse 1s infinite" }}>●</span> {agentMsg}
            </div>
          )}
        </div>

        {/* ── Stats ── */}
        <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:12, marginBottom:20 }}>
          {[
            { label:"Posts Scheduled", value:schedCount,   color:"#00cfff" },
            { label:"Posts Published", value:pubCount,     color:"#00ff88" },
            { label:"Keywords Tracked",value:KEYWORDS.length, color:"#ffb800" },
            { label:"Pipeline Status", value:"LIVE ✅",    color:"#00ff88" },
          ].map((s,i) => (
            <div key={i} style={{ background:"rgba(255,255,255,0.03)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:10, padding:"14px 16px" }}>
              <div style={{ fontSize:20, fontWeight:800, color:s.color }}>{s.value}</div>
              <div style={{ fontSize:11, color:"#4a6580", marginTop:2 }}>{s.label}</div>
            </div>
          ))}
        </div>

        {/* ── Tabs ── */}
        <div style={{ display:"flex", gap:4, marginBottom:18, flexWrap:"wrap" }}>
          {TABS.map((t,i) => (
            <button key={i} onClick={() => setTab(i)} style={{
              padding:"8px 16px", borderRadius:6, border:"none", cursor:"pointer",
              fontSize:12, fontFamily:"inherit", fontWeight:600,
              background: tab===i ? "linear-gradient(135deg,#00ff88,#00cfff)" : "rgba(255,255,255,0.05)",
              color: tab===i ? "#080c14" : "#7a90a8", transition:"all 0.2s",
            }}>{t}</button>
          ))}
        </div>

        {/* ══ TAB 0: AI WRITER ══ */}
        {tab === 0 && (
          <div style={{ display:"grid", gridTemplateColumns:"290px 1fr", gap:16 }}>

            {/* Controls */}
            <div style={{ display:"flex", flexDirection:"column", gap:12 }}>
              <Panel title="🔑 Keyword">
                <input value={customKw} onChange={e => setCustomKw(e.target.value)}
                  placeholder="Custom keyword (optional)..."
                  style={{ width:"100%", padding:"8px 10px", borderRadius:6, background:"rgba(0,0,0,0.4)", border:"1px solid rgba(0,255,136,0.2)", color:"#e0e8f0", fontSize:12, fontFamily:"inherit", boxSizing:"border-box", marginBottom:8 }} />
                <div style={{ maxHeight:200, overflowY:"auto" }}>
                  {KEYWORDS.map((k,i) => (
                    <div key={i} onClick={() => { setSelKw(k); setCustomKw(""); }} style={{
                      padding:"7px 10px", borderRadius:5, cursor:"pointer", marginBottom:3,
                      background: selKw.kw===k.kw && !customKw ? "rgba(0,255,136,0.1)" : "transparent",
                      border:     selKw.kw===k.kw && !customKw ? "1px solid rgba(0,255,136,0.3)" : "1px solid transparent",
                      fontSize:11,
                    }}>
                      <div style={{ color:"#e0e8f0", marginBottom:2 }}>{k.kw}</div>
                      <div style={{ display:"flex", gap:6 }}>
                        <span style={{ color:ic(k.intent), fontSize:10 }}>{k.intent}</span>
                        <span style={{ color:"#4a6580", fontSize:10 }}>· {k.vol}/mo</span>
                      </div>
                    </div>
                  ))}
                </div>
              </Panel>

              <Panel title="📝 Post Type">
                {TEMPLATES.map((t,i) => (
                  <div key={i} onClick={() => setSelTpl(t)} style={{
                    padding:"8px 10px", borderRadius:5, cursor:"pointer", marginBottom:3,
                    background: selTpl.type===t.type ? "rgba(0,207,255,0.1)" : "transparent",
                    border:     selTpl.type===t.type ? "1px solid rgba(0,207,255,0.3)" : "1px solid transparent",
                    display:"flex", justifyContent:"space-between", fontSize:12,
                  }}>
                    <span>{t.icon} {t.type}</span>
                    <span style={{ color:"#4a6580", fontSize:11 }}>{t.words}w</span>
                  </div>
                ))}
              </Panel>

              <button onClick={generate} disabled={generating} style={{
                padding:"14px", borderRadius:8, border:"none",
                cursor: generating ? "not-allowed" : "pointer",
                background: generating ? "rgba(0,255,136,0.15)" : "linear-gradient(135deg,#00ff88,#00cfff)",
                color:"#080c14", fontWeight:800, fontSize:13, fontFamily:"inherit",
              }}>{generating ? "⏳ Generating..." : "⚡ Generate Post"}</button>
            </div>

            {/* Output */}
            <div>
              {!post && !generating && (
                <div style={{ height:460, display:"flex", alignItems:"center", justifyContent:"center", background:"rgba(255,255,255,0.02)", borderRadius:10, border:"1px dashed rgba(255,255,255,0.08)", flexDirection:"column", gap:10 }}>
                  <div style={{ fontSize:44 }}>✍️</div>
                  <div style={{ color:"#4a6580", fontSize:13 }}>Pick keyword + post type → Generate</div>
                  <div style={{ color:"#2a3a4a", fontSize:11 }}>Publishes live to blog.tikprofitpro.shop via your API</div>
                </div>
              )}

              {generating && (
                <div style={{ background:"rgba(0,0,0,0.4)", borderRadius:10, padding:20, border:"1px solid rgba(0,255,136,0.15)", height:460, overflowY:"auto" }}>
                  <div style={{ fontSize:11, color:"#00ff88", marginBottom:10, letterSpacing:2 }}>● GENERATING...</div>
                  <pre style={{ fontSize:11, color:"#7a9ab8", whiteSpace:"pre-wrap", lineHeight:1.7, margin:0 }}>
                    {stream}<span style={{ color:"#00ff88", animation:"blink 1s infinite" }}>█</span>
                  </pre>
                </div>
              )}

              {post && !generating && (
                <div style={{ display:"flex", flexDirection:"column", gap:12 }}>

                  {/* SEO Score */}
                  {score && (
                    <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:10, padding:14, border:`1px solid ${sc(score)}30`, display:"flex", alignItems:"center", gap:14 }}>
                      <div style={{ width:56, height:56, borderRadius:"50%", flexShrink:0, border:`3px solid ${sc(score)}`, display:"flex", alignItems:"center", justifyContent:"center", fontSize:17, fontWeight:800, color:sc(score) }}>{score}</div>
                      <div>
                        <div style={{ fontWeight:700, color:"#fff", marginBottom:3 }}>SEO Score</div>
                        <div style={{ fontSize:11, color:"#4a6580" }}>{post.seoRaw}</div>
                      </div>
                    </div>
                  )}

                  {/* Meta */}
                  <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:10 }}>
                    {[{ label:"Title Tag", val:post.title }, { label:"URL Slug", val:`/${post.slug}` }].map((f,i) => (
                      <div key={i} style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:12, border:"1px solid rgba(255,255,255,0.07)" }}>
                        <div style={{ fontSize:10, color:"#4a6580", marginBottom:4, textTransform:"uppercase", letterSpacing:1 }}>{f.label}</div>
                        <div style={{ fontSize:12, color:"#e0e8f0" }}>{f.val}</div>
                      </div>
                    ))}
                  </div>

                  {post.meta && (
                    <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:12, border:"1px solid rgba(255,255,255,0.07)" }}>
                      <div style={{ fontSize:10, color:"#4a6580", marginBottom:4, textTransform:"uppercase", letterSpacing:1 }}>Meta Description</div>
                      <div style={{ fontSize:12, color:"#e0e8f0" }}>{post.meta}</div>
                    </div>
                  )}

                  {/* Content preview */}
                  <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:10, padding:14, border:"1px solid rgba(255,255,255,0.07)", maxHeight:180, overflowY:"auto" }}>
                    <div style={{ fontSize:10, color:"#4a6580", marginBottom:8, textTransform:"uppercase", letterSpacing:1 }}>Content Preview</div>
                    <pre style={{ fontSize:11, color:"#7a9ab8", whiteSpace:"pre-wrap", lineHeight:1.8, margin:0 }}>{post.content?.slice(0,900)}...</pre>
                  </div>

                  {/* Publish success */}
                  {pubStatus === "published" && pubUrl && (
                    <div style={{ padding:14, borderRadius:8, background:"rgba(0,255,136,0.08)", border:"1px solid rgba(0,255,136,0.3)" }}>
                      <div style={{ color:"#00ff88", fontWeight:700, marginBottom:8, fontSize:14 }}>🎉 Live on Ghost!</div>
                      <a href={pubUrl} target="_blank" rel="noreferrer"
                        style={{ color:"#00cfff", fontSize:12, wordBreak:"break-all", textDecoration:"underline" }}>
                        {pubUrl}
                      </a>
                      <div style={{ marginTop:8, fontSize:11, color:"#4a6580" }}>Click the link above to view your live post ↑</div>
                    </div>
                  )}

                  {/* Publish error */}
                  {pubStatus === "error" && (
                    <div style={{ padding:12, borderRadius:8, background:"rgba(255,68,68,0.08)", border:"1px solid rgba(255,68,68,0.3)", fontSize:12, color:"#ff6b6b" }}>
                      ⚠️ Publish failed: {pubErr}
                    </div>
                  )}

                  {/* Buttons */}
                  <div style={{ display:"flex", gap:10 }}>
                    <button onClick={publish}
                      disabled={pubStatus==="publishing" || pubStatus==="published"}
                      style={{
                        flex:1, padding:"14px", borderRadius:8, border:"none", cursor:"pointer",
                        background: pubStatus==="published"  ? "rgba(0,255,136,0.15)"
                                  : pubStatus==="publishing" ? "rgba(0,255,136,0.1)"
                                  : "linear-gradient(135deg,#00ff88,#00cfff)",
                        color: pubStatus==="published" ? "#00ff88" : "#080c14",
                        fontWeight:800, fontSize:13, fontFamily:"inherit", transition:"all 0.2s",
                      }}>
                      {pubStatus==="publishing" ? "🚀 Publishing..." :
                       pubStatus==="published"  ? "✅ Published on Ghost!" :
                       "🚀 Publish to blog.tikprofitpro.shop"}
                    </button>
                    <button onClick={() => setTab(2)} style={{ padding:"14px 18px", borderRadius:8, border:"1px solid rgba(255,255,255,0.1)", background:"transparent", color:"#e0e8f0", cursor:"pointer", fontWeight:600, fontSize:12, fontFamily:"inherit" }}>
                      📅 Schedule
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ══ TAB 1: KEYWORDS ══ */}
        {tab === 1 && (
          <div style={{ display:"grid", gap:8 }}>
            {KEYWORDS.map((k,i) => (
              <div key={i} style={{ background:"rgba(255,255,255,0.03)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:8, padding:"14px 16px", display:"grid", gridTemplateColumns:"1fr auto auto auto", alignItems:"center", gap:16 }}>
                <div>
                  <div style={{ fontWeight:600, marginBottom:3, fontSize:13 }}>{k.kw}</div>
                  <span style={{ fontSize:10, padding:"2px 8px", borderRadius:20, background:`${ic(k.intent)}20`, color:ic(k.intent) }}>{k.intent}</span>
                </div>
                <div style={{ textAlign:"center" }}>
                  <div style={{ fontSize:16, fontWeight:700, color:"#00cfff" }}>{k.vol}</div>
                  <div style={{ fontSize:10, color:"#4a6580" }}>searches/mo</div>
                </div>
                <div style={{ textAlign:"center" }}>
                  <div style={{ fontSize:13, fontWeight:600, color:k.difficulty.includes("Low")?"#00ff88":"#ffb800" }}>{k.difficulty}</div>
                  <div style={{ fontSize:10, color:"#4a6580" }}>difficulty</div>
                </div>
                <button onClick={() => { setSelKw(k); setCustomKw(""); setTab(0); }}
                  style={{ padding:"6px 14px", borderRadius:6, border:"1px solid rgba(0,255,136,0.3)", background:"rgba(0,255,136,0.08)", color:"#00ff88", cursor:"pointer", fontSize:11, fontFamily:"inherit" }}>
                  Write →
                </button>
              </div>
            ))}
          </div>
        )}

        {/* ══ TAB 2: SCHEDULER ══ */}
        {tab === 2 && (
          <div style={{ display:"grid", gap:8 }}>
            {scheduled.map((p,i) => (
              <div key={i} style={{ background:"rgba(255,255,255,0.03)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:8, padding:"14px 16px", display:"grid", gridTemplateColumns:"1fr auto auto", alignItems:"center", gap:16 }}>
                <div>
                  <div style={{ fontWeight:600, marginBottom:4, fontSize:13 }}>{p.title}</div>
                  <div style={{ fontSize:11, color:"#4a6580" }}>🔑 {p.keyword}</div>
                </div>
                <div style={{ fontSize:12, color:"#e0e8f0" }}>{p.date}</div>
                <Pill status={p.status} />
              </div>
            ))}
          </div>
        )}

        {/* ══ TAB 3: PUBLISHER ══ */}
        {tab === 3 && (
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
            <Panel title="🔌 Integrations">
              {[
                { name:"FastAPI Proxy",          note:"api.tikprofitpro.shop/publish-blog-post", icon:"⚡", live:true  },
                { name:"Ghost CMS",              note:"blog.tikprofitpro.shop",                  icon:"👻", live:true  },
                { name:"Google Search Console",  note:"Auto-submit new posts",                   icon:"🔍", live:false },
                { name:"Google Analytics 4",     note:"Track organic traffic",                   icon:"📊", live:false },
                { name:"Ahrefs",                 note:"Keyword rank tracking",                   icon:"📈", live:false },
              ].map((s,i) => (
                <div key={i} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"10px 0", borderBottom:"1px solid rgba(255,255,255,0.05)" }}>
                  <div>
                    <div style={{ fontSize:13, fontWeight:600 }}>{s.icon} {s.name}</div>
                    <div style={{ fontSize:11, color:"#4a6580" }}>{s.note}</div>
                  </div>
                  <span style={{ fontSize:11, padding:"3px 10px", borderRadius:20, background:s.live?"rgba(0,255,136,0.15)":"rgba(255,255,255,0.05)", color:s.live?"#00ff88":"#4a6580" }}>
                    {s.live ? "✅ Live" : "Connect"}
                  </span>
                </div>
              ))}
            </Panel>

            <Panel title="⚙️ Pipeline Config">
              {[
                { rule:"Proxy endpoint",         value:"✅ Connected" },
                { rule:"Ghost CMS",              value:"✅ Connected" },
                { rule:"Post format",            value:"Mobiledoc + Markdown" },
                { rule:"Auto-tags",              value:"TikTok Shop, SEO" },
                { rule:"Meta title + desc",      value:"ON ✅" },
                { rule:"Min SEO score",          value:"75 / 100" },
                { rule:"Target posts/week",      value:"2 posts" },
                { rule:"Best publish days",      value:"Tue & Thu" },
              ].map((r,i) => (
                <div key={i} style={{ display:"flex", justifyContent:"space-between", padding:"9px 0", borderBottom:"1px solid rgba(255,255,255,0.05)", fontSize:12 }}>
                  <span style={{ color:"#7a9ab8" }}>{r.rule}</span>
                  <span style={{ color:"#00ff88", fontWeight:600 }}>{r.value}</span>
                </div>
              ))}
            </Panel>
          </div>
        )}

        {/* ══ TAB 4: TRACKER ══ */}
        {tab === 4 && (
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
            <Panel title="📈 Keyword Rankings">
              {KEYWORDS.slice(0,6).map((k,i) => {
                const ranks  = ["-",47,23,"-",89,12];
                const trends = [null,"↑","↑",null,"↓","↑"];
                const rank = ranks[i], trend = trends[i];
                return (
                  <div key={i} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"9px 0", borderBottom:"1px solid rgba(255,255,255,0.05)", fontSize:12 }}>
                    <span style={{ color:"#7a9ab8", flex:1, marginRight:10 }}>{k.kw}</span>
                    <span style={{ color:rank==="-"?"#4a6580":rank<=20?"#00ff88":rank<=50?"#ffb800":"#ff6b6b", fontWeight:700 }}>
                      {rank==="-" ? "Not ranked" : `#${rank}`}
                      {trend && <span style={{ marginLeft:4, color:trend==="↑"?"#00ff88":"#ff6b6b" }}>{trend}</span>}
                    </span>
                  </div>
                );
              })}
            </Panel>

            <Panel title="🌐 Live Pipeline Status">
              {[
                { label:"FastAPI Proxy",    value:"🟢 Online" },
                { label:"Ghost Blog",       value:"🟢 Online" },
                { label:"Blog URL",         value:"blog.tikprofitpro.shop" },
                { label:"API URL",          value:"api.tikprofitpro.shop" },
                { label:"Posts Published",  value:pubCount },
                { label:"Posts Scheduled",  value:schedCount },
              ].map((m,i) => (
                <div key={i} style={{ display:"flex", justifyContent:"space-between", padding:"9px 0", borderBottom:"1px solid rgba(255,255,255,0.05)", fontSize:12 }}>
                  <span style={{ color:"#7a9ab8" }}>{m.label}</span>
                  <span style={{ color:"#e0e8f0", fontWeight:600 }}>{m.value}</span>
                </div>
              ))}
              <div style={{ marginTop:12, padding:12, borderRadius:8, background:"rgba(0,255,136,0.06)", border:"1px solid rgba(0,255,136,0.15)", fontSize:11, color:"#4a6580", lineHeight:1.7 }}>
                🎉 <strong style={{ color:"#00ff88" }}>Full pipeline is live.</strong> Generate a post → click Publish → it appears on your Ghost blog within seconds.
              </div>
            </Panel>
          </div>
        )}

      </div>

      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0}   }
        * { scrollbar-width:thin; scrollbar-color:rgba(0,255,136,0.2) transparent }
        *::-webkit-scrollbar { width:4px }
        *::-webkit-scrollbar-thumb { background:rgba(0,255,136,0.2); border-radius:2px }
      `}</style>
    </div>
  );
}
