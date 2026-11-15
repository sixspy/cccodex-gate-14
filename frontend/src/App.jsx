import { useEffect, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}, token = null) {
  const bearer = token ?? (localStorage.getItem("lake.token") || "");
  const headers = { ...(options.headers || {}) };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

function cutChip(method, label) {
  if (!method) return <span className="chip unset">{label || "尚未按分区落库"}</span>;
  return (
    <span className={method === "event" ? "chip event" : "chip channel"}>
      {label || (method === "event" ? "这一批按事件码切开" : "这一批按通道切开")}
    </span>
  );
}

function facetChip(row) {
  if (row.facet_axis === "empty") return <b className="clash">空</b>;
  if (row.facet_axis === "event") return <b className="ok">事件码切面</b>;
  if (row.facet_axis === "channel") return <b className="ok">通道切面</b>;
  return <b className="clash">别的切面名</b>;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [desk, setDesk] = useState(null);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [method, setMethod] = useState("event");
  const [carried, setCarried] = useState("");
  const [adoptFacet, setAdoptFacet] = useState("event");
  const [scanForm, setScanForm] = useState({
    biz_date: "2026-11-13",
    gate: "T1",
    scan: "IN",
    box: "OOLU1234567",
    at: "2026-11-13T08:00:00",
    facet: "T1",
  });

  async function loadDay(target) {
    const [dayDetail, dayDesk] = await Promise.all([
      request(`/api/dates/${target}`),
      request("/api/partitions"),
    ]);
    setDetail(dayDetail);
    setDesk(dayDesk);
    setCarried(dayDetail.facets?.batch_cut_method || dayDesk.settings.method || "");
  }

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[rows.length - 1]?.biz_date;
    if (target) await loadDay(target);
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request(
        "/api/login",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password }),
        },
        "",
      );
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function pin(event) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request("/api/partitions/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method }),
      });
      setNotice(`分区切法已封存：${data.method_name}，全湖沿用同一套切法。`);
    } catch (error) {
      setFormError(error.message);
    } finally {
      await refresh(detail?.biz_date).catch(() => {});
      setBusy(false);
    }
  }

  async function build() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method: carried }),
      });
      setNotice(
        `${data.summary.batch_cut}：收 ${data.summary.accepted} 条，剔 ${data.summary.rejected} 条，混切未落 ${data.summary.mixed_kept} 条。`,
      );
    } catch (error) {
      setFormError(error.message);
    } finally {
      await refresh(detail.biz_date).catch(() => {});
      setBusy(false);
    }
  }

  async function adopt(event) {
    event.preventDefault();
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request("/api/mixed/adopt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ biz_date: detail.biz_date, facet: adoptFacet }),
      });
      setNotice(`${data.note}（${data.adopted} 条）。`);
    } catch (error) {
      setFormError(error.message);
    } finally {
      await refresh(detail.biz_date).catch(() => {});
      setBusy(false);
    }
  }

  async function land(event) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request("/api/scans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          biz_date: scanForm.biz_date,
          payload: {
            gate: scanForm.gate,
            scan: scanForm.scan,
            box: scanForm.box,
            at: scanForm.at,
            facet: scanForm.facet,
          },
        }),
      });
      setNotice(
        `扫描已落贴源：切面「${data.facet.facet_name || "（空）"}」— ${data.facet.status_name}。`,
      );
    } catch (error) {
      setFormError(error.message);
    } finally {
      await refresh(scanForm.biz_date).catch(() => {});
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setDesk(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  const writer = profile.role === "writer";
  const settings = desk?.settings;
  const sealed = Boolean(settings?.sealed);
  const dayQueue = (desk?.queue || []).filter((row) => row.biz_date === detail?.biz_date);
  const dayAttempts = (desk?.attempts || []).filter((row) => row.biz_date === detail?.biz_date);

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => loadDay(row.biz_date).catch((error) => setFormError(error.message))}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              <span>
                事件面 {row.event_facets} · 通道面 {row.channel_facets} · 混切 {row.mixed_count}
              </span>
              <span>{row.batch_cut}</span>
            </button>
          ))}
          {!dates.length ? <p className="note">还没有业务日。</p> : null}
        </aside>

        <div className="stack">
          {formError ? <p className="warn sheet-line">{formError}</p> : null}
          {notice ? <p className="notice sheet-line">{notice}</p> : null}

          {detail && desk ? (
            <section className="sheet">
              <div className="split-head">
                <h2>{detail.biz_date}</h2>
                {cutChip(detail.facets.batch_cut_method, detail.facets.batch_cut)}
              </div>
              <p className="note">
                分区切法：{settings.method_name || "尚未封存"}
                {sealed ? ` · 由 ${settings.pinned_by} 于 ${settings.pinned_at} 封存，本日禁止改口。` : "。"}
              </p>
              <div className="actions">
                <label className="carry">
                  收成携带的切法
                  <input value={carried} onChange={(event) => setCarried(event.target.value)} />
                </label>
                <button disabled={!writer || busy || !sealed} onClick={build}>
                  按分区切面收成
                </button>
                {!sealed ? <span className="note">分区切面尚未锁死，这一批不准落库。</span> : null}
              </div>
              {!writer ? <p className="note">核对员不能重新构建。</p> : null}

              <div className="layers">
                <article>
                  <h3>贴源层</h3>
                  <ul>
                    {detail.ods.map((row) => (
                      <li key={row.id}>
                        <span>{row.terminal_code}</span>
                        <code>{JSON.stringify(row.raw_payload)}</code>
                      </li>
                    ))}
                  </ul>
                </article>
                <article>
                  <h3>明细层</h3>
                  {detail.run?.method ? (
                    <p className="note">
                      上次收成：{detail.run.method_name} · 收 {detail.run.accepted_count} 剔 {detail.run.rejected_count}
                      {detail.run.deviation ? " · 本次携带的切面偏了" : ""}
                    </p>
                  ) : null}
                  {detail.moves.length ? (
                    <table>
                      <thead>
                        <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th><th>分区</th></tr>
                      </thead>
                      <tbody>
                        {detail.moves.map((row) => (
                          <tr key={`${row.source_id}-${row.event_kind}`}>
                            <td>{row.source_id}</td>
                            <td>{row.container_no}</td>
                            <td>{row.event_kind}</td>
                            <td>{row.event_at}</td>
                            <td>{row.partition_key || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : <p>这一天的明细还是空的。</p>}
                  <h3>剔除</h3>
                  <ul>
                    {detail.rejects.map((row) => (
                      <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                    ))}
                    {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                  </ul>
                </article>
              </div>
            </section>
          ) : null}

          {desk ? (
            <section className="sheet desk">
              <div className="split-head">
                <h2>分区台</h2>
                <span className="note">分区列表 · 切面详情 · 分区设置 · 混切队列</span>
              </div>
              <div className="desk-grid">
                <article className="card">
                  <h3>分区列表</h3>
                  <table>
                    <thead>
                      <tr><th>业务日</th><th>事件面数</th><th>通道面数</th><th>混切次数</th><th>办法名</th></tr>
                    </thead>
                    <tbody>
                      {desk.days.map((row) => (
                        <tr key={row.biz_date}>
                          <td>{row.biz_date}</td>
                          <td>{row.event_facets}</td>
                          <td>{row.channel_facets}</td>
                          <td>{row.mixed_count}</td>
                          <td>{row.method_name}</td>
                        </tr>
                      ))}
                      {!desk.days.length ? <tr><td colSpan={5}>还没有业务日</td></tr> : null}
                    </tbody>
                  </table>
                  <p className="note">
                    全湖合计：事件面 {desk.summary.event_facets} · 通道面 {desk.summary.channel_facets} ·
                    混切 {desk.summary.mixed_count} · 办法名 {desk.summary.method_name}
                  </p>
                </article>

                <article className="card">
                  <h3>分区设置</h3>
                  {sealed ? (
                    <p className="note">
                      已封存：{settings.method_name}，由 {settings.pinned_by} 于 {settings.pinned_at} 选定。
                      切面选定后即封存，本日禁止改口，全湖沿用同一套切法。
                    </p>
                  ) : (
                    <form onSubmit={pin}>
                      <label className="radio">
                        <input type="radio" checked={method === "event"} onChange={() => setMethod("event")} />
                        按事件码切开后落库
                      </label>
                      <label className="radio">
                        <input type="radio" checked={method === "channel"} onChange={() => setMethod("channel")} />
                        按通道切开后落库
                      </label>
                      <button disabled={!writer || busy}>封存这一套切法</button>
                    </form>
                  )}
                </article>

                <article className="card wide">
                  <h3>切面详情</h3>
                  <p className="note">
                    对照同一条扫描按事件码切、按通道切各落到哪个分区，以及它实际落在哪。
                  </p>
                  <table>
                    <thead>
                      <tr>
                        <th>来源</th><th>通道</th><th>切面名</th><th>切面属</th>
                        <th>事件码切</th><th>通道切</th><th>实际落点</th><th>结论</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail?.facets?.rows.map((row) => (
                        <tr key={row.source_id}>
                          <td>{row.source_id}</td>
                          <td>{row.terminal_code}</td>
                          <td>{row.facet_name || "（空）"}</td>
                          <td>{facetChip(row)}</td>
                          <td><code>{row.event_key || "—"}</code></td>
                          <td><code>{row.channel_key || "—"}</code></td>
                          <td><code>{row.landed_key || "—"}</code></td>
                          <td>{row.status_name}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="note">
                    {detail?.facets?.batch_cut || "这一批尚未落库"}。
                    {detail?.facets?.partitions.length
                      ? `分区：${detail.facets.partitions
                          .map((item) => `${item.partition_key}（${item.rows} 行）`)
                          .join("，")}。`
                      : ""}
                  </p>
                </article>

                <article className="card">
                  <h3>混切队列</h3>
                  {sealed ? (
                    <form onSubmit={adopt}>
                      <label>
                        带上的切面
                        <select value={adoptFacet} onChange={(event) => setAdoptFacet(event.target.value)}>
                          <option value="event">按事件码切</option>
                          <option value="channel">按通道切</option>
                          <option value="">（空切面不进队列）</option>
                        </select>
                      </label>
                      <button disabled={!writer || busy || !dayQueue.length}>按分区设置采纳这一批</button>
                      {!dayQueue.length ? <p className="note">这一日没有混切扫描。</p> : null}
                    </form>
                  ) : (
                    <p className="note">分区切面尚未锁死，混切队列不采纳。</p>
                  )}
                  <ul>
                    {dayQueue.map((row) => (
                      <li key={row.id}>
                        <span>
                          <b className={row.outcome === "adopted" ? "ok" : row.outcome === "pending" ? "" : "clash"}>
                            {row.outcome_name}
                          </b>
                          {" "}来源 {row.ods_id} · {row.terminal_code} · 切面「{row.facet_name}」
                        </span>
                        <em>
                          事件码切 {row.event_key || "—"} / 通道切 {row.channel_key || "—"}
                          {row.settle_note ? ` · ${row.settle_note}` : ""}
                        </em>
                      </li>
                    ))}
                    {!dayQueue.length ? <li>这一日混切队列是空的</li> : null}
                  </ul>
                </article>

                <article className="card">
                  <h3>混切台账</h3>
                  <ul>
                    {dayAttempts.map((row) => (
                      <li key={row.id}>
                        <span>
                          <b className={row.outcome === "adopted" ? "ok" : "clash"}>{row.outcome_name}</b>
                          {" "}带上的切面「{row.carried_facet || "（空）"}」
                        </span>
                        <em>{row.note} · {row.actor} · {row.created_at}</em>
                      </li>
                    ))}
                    {!dayAttempts.length ? <li>这一日还没有混切处置记录</li> : null}
                  </ul>
                </article>

                {writer ? (
                  <article className="card">
                    <h3>落一条扫描</h3>
                    <form onSubmit={land} className="scan">
                      <label>业务日<input value={scanForm.biz_date} onChange={(e) => setScanForm({ ...scanForm, biz_date: e.target.value })} /></label>
                      <label>通道<input value={scanForm.gate} onChange={(e) => setScanForm({ ...scanForm, gate: e.target.value })} /></label>
                      <label>事件码<input value={scanForm.scan} onChange={(e) => setScanForm({ ...scanForm, scan: e.target.value })} /></label>
                      <label>箱号<input value={scanForm.box} onChange={(e) => setScanForm({ ...scanForm, box: e.target.value })} /></label>
                      <label>扫描时刻<input value={scanForm.at} onChange={(e) => setScanForm({ ...scanForm, at: e.target.value })} /></label>
                      <label>切面名<input value={scanForm.facet} onChange={(e) => setScanForm({ ...scanForm, facet: e.target.value })} /></label>
                      <button disabled={busy}>落贴源</button>
                    </form>
                    <p className="note">
                      切面名跟封存切法对不上就进混切队列；切面名为空的不进队列。
                    </p>
                  </article>
                ) : null}
              </div>
            </section>
          ) : null}
        </div>
      </main>
    </>
  );
}
