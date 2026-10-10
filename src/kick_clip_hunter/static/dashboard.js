// Clip Hunter dashboard.
//
// The pages are rendered on the server and work as plain pages. This script
// makes them quick to use: it opens a moment from the queue without loading
// the page again, saves a rating, tag or note as it is set, drives the clip
// player from the strip under it, replays chat in step with the clip, runs
// the review from the keyboard (1 to 5 rate, J and K move, Space plays), and
// has the dialog a stretch of the footage is cut out for an editor in.
//
// Everything is wired up by delegation from the document, because the open
// moment's markup is replaced every time another one is opened.
(() => {
  "use strict";

  const one = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;

  // The response, or null when the service could not be reached at all.
  async function post(url, body) {
    const options = { method: "POST", keepalive: true };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    try {
      return await fetch(url, options);
    } catch {
      return null;
    }
  }

  // Says something in the status line of whatever part of the page `from`
  // belongs to. Failures are stated there in words; an empty string clears it.
  function say(from, words) {
    const scope = from.closest("[data-status-scope]");
    const line = scope && one("[data-status]", scope);
    if (line) line.textContent = words;
  }

  // ---- What is new ------------------------------------------------------

  // The page never reloads on its own - that kept interrupting whatever was
  // being watched or typed. It asks how many moments and clips there are
  // now and offers the difference in the queue instead.
  const NEWS_INTERVAL_MS = 30000;

  async function checkForNew() {
    const news = one("[data-news]");
    let status;
    try {
      const response = await fetch("/moments/status");
      if (!response.ok) return;
      status = await response.json();
    } catch {
      return; // restarting or down - keep whatever the banner says
    }
    const moments = status.moments - Number(news.dataset.moments);
    const clips = status.clips - Number(news.dataset.clips);
    const parts = [];
    if (moments > 0) parts.push(plural(moments, "new moment"));
    if (clips > 0) parts.push(plural(clips, "new clip"));
    news.hidden = parts.length === 0;
    one("[data-news-words]", news).textContent = `${parts.join(" and ")} since you opened this page`;
  }

  // ---- The queue --------------------------------------------------------

  const rows = () => all("a.ch-row");
  const currentRow = () => one('a.ch-row[aria-current="true"]');
  const isPlainClick = (event) =>
    event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;

  let opening = 0; // counts requests to open a moment, so only the latest one lands
  let loading = 0; // how many of them are still on their way

  // Opens the moment of a queue row beside the queue. The row is an ordinary
  // link to the same thing, which is what happens if this cannot be done.
  async function openMoment(row, { play = false } = {}) {
    const ticket = ++opening;
    loading += 1;
    let markup = null;
    try {
      await saveNote();
      await saveTitle();
      const response = await fetch(`/dashboard/moments/${row.dataset.moment}`);
      if (response.ok) markup = await response.text();
    } catch {
      // handled below, like any other answer that is not the moment
    } finally {
      loading -= 1;
    }
    if (ticket !== opening) return;
    if (markup === null) {
      location.assign(row.href);
      return;
    }

    one("[data-open]").innerHTML = markup;
    for (const other of rows()) other.removeAttribute("aria-current");
    row.setAttribute("aria-current", "true");
    row.scrollIntoView({ block: "nearest" });
    history.replaceState(null, "", row.href);
    dress();
    if (play) one("[data-clip]")?.play().catch(() => {});
  }

  function move(step) {
    const list = rows();
    if (!list.length) return;
    const index = list.indexOf(currentRow());
    const row = index < 0 ? list[step > 0 ? 0 : list.length - 1] : list[index + step];
    if (row) openMoment(row, { play: true });
  }

  // The next row down that still wants a rating, or failing that the first
  // one above.
  function nextUnrated() {
    const list = rows();
    const index = list.indexOf(currentRow());
    const unrated = (row) => row.classList.contains("is-unrated");
    return list.slice(index + 1).find(unrated) || list.slice(0, Math.max(index, 0)).find(unrated) || null;
  }

  // ---- Rating, tags, the note ------------------------------------------

  // The counts on the queue's tabs are totals for the channel it is narrowed
  // to, so they follow a rating only when the moment is from that channel.
  function recount(article, before, rating) {
    const queue = one("[data-queue]");
    if (!queue || (queue.dataset.channel && queue.dataset.channel !== article.dataset.channel)) return;
    const best = Number(queue.dataset.bestFrom);
    const bump = (show, by) => {
      const count = one(`[data-show="${show}"] [data-count]`, queue);
      if (count && by) count.textContent = String(Math.max(0, Number(count.textContent) + by));
    };
    bump("unrated", Number(rating === 0) - Number(before === 0));
    bump("best", Number(rating >= best) - Number(before >= best));
  }

  // Sets the rating, or clears it when the chosen key is pressed again.
  // Returns the rating now stored (0 for none), or null if it was not saved.
  async function rate(article, value) {
    const before = Number(article.dataset.rating);
    const rating = before === value ? 0 : value;
    const response = await post(`/moments/${article.dataset.moment}/rating?value=${rating}`);
    if (!response || !response.ok) {
      say(article, "Could not save the rating. Try again.");
      return null;
    }
    say(article, "");
    article.dataset.rating = String(rating);

    const keys = one("[data-rating-keys]", article);
    keys.setAttribute("aria-label", `Rating, ${rating ? `${rating} of 5` : "not rated yet"}`);
    for (const key of all("[data-rate]", keys)) {
      const number = Number(key.dataset.rate);
      key.classList.toggle("is-lit", number <= rating);
      key.setAttribute("aria-pressed", String(number === rating));
    }

    const row = one(`a.ch-row[data-moment="${article.dataset.moment}"]`);
    if (row) {
      row.classList.toggle("is-unrated", rating === 0);
      const meter = one(".ch-meter", row);
      meter.setAttribute("aria-label", rating ? `rated ${rating} of 5` : "not rated yet");
      all("i", meter).forEach((segment, index) => segment.classList.toggle("is-lit", index < rating));
    }
    recount(article, before, rating);
    return rating;
  }

  // A number key rates the open moment and moves on to the next unrated one.
  async function rateFromKeyboard(value) {
    const article = one(".moment");
    if (!article || loading) return;
    const rating = await rate(article, value);
    if (!rating) return;
    const next = nextUnrated();
    if (next) openMoment(next, { play: true });
  }

  // One tag per group; choosing the lit one again clears it.
  async function setTag(button) {
    const article = button.closest(".moment");
    const field = button.dataset.tag;
    const clearing = button.getAttribute("aria-pressed") === "true";
    const value = clearing ? "" : button.dataset.value;
    const response = await post(`/moments/${article.dataset.moment}/${field}?value=${encodeURIComponent(value)}`);
    if (!response || !response.ok) {
      say(article, "Could not save the tag. Try again.");
      return;
    }
    say(article, "");
    for (const other of all(`[data-tag="${field}"]`, article)) {
      other.setAttribute("aria-pressed", String(other === button && !clearing));
    }
  }

  // Saves the note if it was changed since it was last saved. Called when
  // the field is left, and before the moment it belongs to is swapped out.
  async function saveNote(field = one("[data-note]")) {
    if (!field || field.value === field.defaultValue) return;
    const article = field.closest(".moment");
    const saved = field.value;
    const response = await post(`/moments/${article.dataset.moment}/notes`, { notes: saved });
    if (!response || !response.ok) {
      say(article, "Could not save the note. Try again.");
      return;
    }
    field.defaultValue = saved;
    say(article, "Note saved.");
  }

  // Saves the moment's name if it was changed, and puts it on the moment's
  // row in the queue - or, if the name was taken away, what the row said
  // before it had one.
  async function saveTitle(field = one("[data-title]")) {
    if (!field || field.value === field.defaultValue) return;
    const article = field.closest(".moment");
    const response = await post(`/moments/${article.dataset.moment}/title`, { title: field.value });
    if (!response || !response.ok) {
      say(article, "Could not save the name. Try again.");
      return;
    }
    const title = (await response.json()).title || "";
    field.value = title;
    field.defaultValue = title;
    say(article, "");
    const what = one(`a.ch-row[data-moment="${article.dataset.moment}"] .ch-what`);
    if (!what) return;
    what.textContent = title ? `${title}${"noClip" in what.dataset ? ", no clip" : ""}` : what.dataset.unnamed;
  }

  // The note is one line until more is typed into it.
  function grow(field) {
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight + field.offsetHeight - field.clientHeight}px`;
  }

  // ---- The clip and its timeline ---------------------------------------
  //
  // The strip under the clip is a timeline over the clip's file, which holds
  // more than the clip: the footage that led up to it comes first and what
  // followed comes after. Times on the strip are clip time - seconds from
  // the clip's first frame, negative before it - so the player's own time
  // is clip time plus however long the footage before the clip runs.

  const SPEEDS = [1, 1.5, 2, 0.5];
  // Kept from one moment to the next - the volume from one visit to the next
  // as well, where the browser lets the page remember it.
  const VOLUME_KEY = "clip-hunter-volume";
  let speed = 1;
  let muted = false;
  let volume = 1;
  try {
    const kept = Number(localStorage.getItem(VOLUME_KEY) ?? 1);
    if (kept >= 0 && kept <= 1) volume = kept;
  } catch {
    // no storage here: the volume starts out full
  }

  const clipOf = (control) => one("[data-clip]", control.closest(".moment"));
  const clock = (seconds) => {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  };

  // What the strip of a moment covers, or null for a clip that has none.
  function spanOf(video) {
    const trace = one("[data-trace]", video.closest(".moment"));
    if (!trace || !one("[data-strip]", trace)) return null;
    return {
      start: Number(trace.dataset.start),
      end: Number(trace.dataset.end),
      clip: Number(trace.dataset.clipSeconds),
    };
  }

  // How long the footage before the clip runs in the player's file.
  const leadOf = (video) => Number(video.dataset.lead) || 0;

  // Where the player is, in clip time.
  function clipTime(video) {
    return video.currentTime - leadOf(video);
  }

  // A clip time the way the page writes it: time into the clip, or how long
  // before its start or after its end.
  function timeWords(time, span) {
    if (span && time < 0) return `−${clock(Math.ceil(-time))}`;
    if (span && time > span.clip) return `+${clock(time - span.clip)}`;
    return clock(time);
  }

  function place(video, time) {
    video.currentTime = Math.max(0, time + leadOf(video));
  }

  // Moves the player to a clip time. The strip can show more of chat than
  // there is footage for; a time beyond the footage is taken to its nearest
  // end.
  function goTo(video, time, { play } = {}) {
    const span = spanOf(video);
    if (!span) return;
    const lead = leadOf(video);
    const last = Number.isFinite(video.duration) ? video.duration - lead : span.end;
    place(video, Math.min(Math.max(time, -lead), last));
    if (play) video.play().catch(() => {});
  }

  // Shows the lines of a box of chat that had arrived by a clip time and
  // no others, the newest at the bottom.
  function reveal(box, time) {
    let changed = false;
    for (const line of box.children) {
      if (line.dataset.at === undefined) continue;
      const later = Number(line.dataset.at) > time;
      if (line.hidden !== later) {
        line.hidden = later;
        changed = true;
      }
    }
    if (changed) box.scrollTop = box.scrollHeight;
  }

  // Chat lines appear as the clip reaches them.
  function syncChat(article, time, words) {
    const box = one("[data-chat]", article);
    if (!box) return;
    reveal(box, time);
    one("[data-chat-foot]", article).textContent = box.querySelector("[data-at]") ? `In step with the clip, at ${words}` : "";
  }

  // Brings the transport, the playhead and the chat in line with the player.
  function paint(video) {
    const article = video.closest(".moment");
    if (!article) return;
    const span = spanOf(video);
    const time = clipTime(video);
    const words = timeWords(time, span);
    const length = span ? span.clip : Number.isFinite(video.duration) ? video.duration : 0;
    const playing = !video.paused && !video.ended;

    one("[data-now]", article).textContent = words;
    one("[data-length]", article).textContent = clock(length);
    const play = one("[data-play]", article);
    play.classList.toggle("is-playing", playing);
    play.setAttribute("aria-label", playing ? "Pause" : "Play");
    one("[data-mute]", article).setAttribute("aria-pressed", String(video.muted));
    const heard = video.muted ? 0 : video.volume;
    const slider = one("[data-volume]", article);
    slider.value = String(heard);
    slider.style.setProperty("--level", `${heard * 100}%`);
    slider.setAttribute("aria-valuetext", `${Math.round(heard * 100)} percent`);
    one("[data-speed]", article).textContent = `${video.playbackRate}×`;
    if (!span) return;

    const strip = one("[data-strip]", article);
    const left = `${Math.min(Math.max((time - span.start) / (span.end - span.start), 0), 1) * 100}%`;
    one("[data-playhead]", strip).style.left = left;
    const flag = one("[data-flag]", strip);
    flag.style.setProperty("--at", left);
    flag.textContent = words;
    strip.setAttribute("aria-valuenow", time.toFixed(1));
    strip.setAttribute("aria-valuetext", `${words} of ${clock(span.clip)}`);
    syncChat(article, time, words);
  }

  function togglePlay(video) {
    if (!video) return;
    // Played to the end of the footage after the clip, it starts over at
    // the clip, not at the footage before it that the file opens with.
    if (video.ended) place(video, 0);
    if (video.paused || video.ended) video.play().catch(() => {});
    else video.pause();
  }

  function setSpeed(video) {
    speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
    video.defaultPlaybackRate = speed;
    video.playbackRate = speed;
  }

  function toggleMute(video) {
    muted = !video.muted;
    video.muted = muted;
    // Taking the mute off a clip that was turned all the way down has to
    // make it heard.
    if (!muted && volume === 0) setVolume(video, 0.5);
  }

  // Dragging the volume up from silence takes the mute off; all the way
  // down is the same as muted.
  function setVolume(video, value) {
    volume = Math.min(Math.max(value, 0), 1);
    muted = volume === 0;
    video.volume = volume;
    video.muted = muted;
    try {
      localStorage.setItem(VOLUME_KEY, String(volume));
    } catch {
      // kept for this page only, then
    }
  }

  function toggleFullScreen(video) {
    if (document.fullscreenElement) document.exitFullscreen();
    else video.requestFullscreen?.().catch(() => {});
  }

  // ---- The strip as a scrubber ------------------------------------------

  // The clip time under the pointer, and how far along the strip that is.
  function pointOn(strip, event) {
    const box = strip.getBoundingClientRect();
    const share = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
    const trace = strip.closest("[data-trace]");
    const start = Number(trace.dataset.start);
    return { share, time: start + share * (Number(trace.dataset.end) - start) };
  }

  // Under the pointer: a hairline, and in the legend what chat did in that second.
  function readOut(strip, event) {
    const { share, time } = pointOn(strip, event);
    const trace = strip.closest("[data-trace]");
    const line = one("[data-hover]", strip);
    line.hidden = false;
    line.style.left = `${share * 100}%`;
    const readout = one("[data-readout]", trace);
    if (!readout) return;
    const second = Math.floor(time - Number(trace.dataset.start));
    const messages = Number(strip.dataset.all.split(",")[second] || 0);
    const laughing = Number(strip.dataset.laugh.split(",")[second] || 0);
    const when = time < 0 ? `${Math.ceil(-time)} s before` : time > Number(trace.dataset.clipSeconds) ? `${Math.floor(time - Number(trace.dataset.clipSeconds))} s after` : clock(time);
    readout.textContent = `${when}: ${plural(messages, "message")} a second${laughing ? `, ${laughing} laughing` : ""}`;
  }

  function stopReadOut(strip) {
    one("[data-hover]", strip).hidden = true;
    const readout = one("[data-readout]", strip.closest("[data-trace]"));
    if (readout) readout.textContent = "";
  }

  // Arrow keys step through the footage, Home and End go to the clip's ends.
  const STRIP_KEYS = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -10, PageUp: 10 };

  function stripKey(strip, event) {
    const video = clipOf(strip);
    const span = spanOf(video);
    let time;
    if (event.key === "Home") time = 0;
    else if (event.key === "End") time = span.clip;
    else if (event.key in STRIP_KEYS) time = clipTime(video) + STRIP_KEYS[event.key] * (event.shiftKey ? 5 : 1);
    else return false;
    event.preventDefault();
    goTo(video, Math.min(Math.max(time, span.start), span.end));
    return true;
  }

  // ---- Where chat sits against the picture ------------------------------

  let shifting = false;

  // Moves a channel's chat earlier or later against its clips, by changing
  // how far behind the broadcast its viewers are taken to be. The trace and
  // the chat lines are drawn on the server, so the moment is fetched again
  // and those parts of it swapped in around the player, which plays on.
  async function shiftChat(button) {
    if (shifting) return;
    shifting = true;
    const article = button.closest(".moment");
    const seconds = Number(button.closest("[data-chat-delay]").dataset.chatDelay) + Number(button.dataset.chatShift);
    let markup = null;
    try {
      const saved = await post(`/channels/${encodeURIComponent(article.dataset.channel)}/chat_delay?seconds=${seconds}`);
      if (saved && saved.ok) {
        const response = await fetch(`/dashboard/moments/${article.dataset.moment}`);
        if (response.ok) markup = await response.text();
      }
    } catch {
      // handled below, like any other answer that is not the moment
    } finally {
      shifting = false;
    }
    if (!article.isConnected) return; // another moment was opened meanwhile
    if (markup === null) {
      say(article, "Could not move the chat. Try again.");
      return;
    }
    say(article, "");

    const held = document.activeElement === button;
    const fresh = new DOMParser().parseFromString(markup, "text/html");
    for (const part of ["[data-trace]", "[data-chat]", "[data-chat-delay]"]) {
      const now = one(part, fresh);
      const old = one(part, article);
      if (now && old) old.replaceWith(now);
    }
    if (held) one(`[data-chat-shift="${button.dataset.chatShift}"]`, article)?.focus();
    const video = one("[data-clip]", article);
    if (video) paint(video);
  }

  // ---- Trimming ---------------------------------------------------------
  //
  // The Trim dialog cuts a stretch of the footage out for an editor, with
  // its chat as a video to lay over it if that is wanted. It has a player
  // of its own on the same file and a copy of the strip, with a handle at
  // either end of the stretch. Times are clip time here too. The cutting is
  // done on the server and takes a while with chat; the dialog asks now and
  // then how far it has got, for as long as it is open.

  const TRIM_POLL_MS = 2000;
  const TRIM_BUSY = ["cutting", "chat"];
  // Past its end by less than this, the player ran there; by more, it was put there.
  const TRIM_RAN_PAST_SECONDS = 1;
  let trimWatch = null;
  let trimDrag = null; // the handle that is held: "from" or "to"
  let trimGrab = 0; // how far from the handle's time it was taken hold of
  let trimWas = 0; // where the dialog's player was when last looked at

  const trimOf = (control) => control.closest("[data-trim]");
  const trimClip = (dialog) => one("[data-trim-clip]", dialog);
  const clamp = (value, least, most) => Math.min(Math.max(value, least), most);

  // What the dialog's strip covers, where the clip lies on it, and how far
  // the footage - what there is to cut from - reaches either way.
  function trimSpan(dialog) {
    const trace = one("[data-trace]", dialog.closest(".moment"));
    const video = trimClip(dialog);
    const lead = Number(dialog.dataset.lead) || 0;
    const start = Number(trace.dataset.start);
    const end = Number(trace.dataset.end);
    const last = Number.isFinite(video.duration) ? video.duration - lead : Number(dialog.dataset.last);
    return { start, end, lead, clip: Number(trace.dataset.clipSeconds), first: Math.max(-lead, start), last: Math.min(last, end) };
  }

  // The stretch that is set, and where the dialog's player is.
  const trimAt = (dialog) => ({ from: Number(dialog.dataset.from), to: Number(dialog.dataset.to) });
  const trimTime = (dialog) => trimClip(dialog).currentTime - (Number(dialog.dataset.lead) || 0);

  // A clip time to the tenth of a second, the way the page writes clip times.
  function trimWords(time, clip) {
    const outside = time < 0 ? "−" : time > clip ? "+" : "";
    const tenths = Math.round(Math.abs(outside === "+" ? time - clip : time) * 10);
    return `${outside}${Math.floor(tenths / 600)}:${String(Math.floor(tenths / 10) % 60).padStart(2, "0")}.${tenths % 10}`;
  }

  // The clip time under the pointer on the dialog's strip - or beyond it,
  // for a pointer that has been dragged off its end.
  function trimPoint(strip, event) {
    const box = strip.getBoundingClientRect();
    const span = trimSpan(trimOf(strip));
    return span.start + ((event.clientX - box.left) / box.width) * (span.end - span.start);
  }

  function seekTrim(dialog, time) {
    const span = trimSpan(dialog);
    trimClip(dialog).currentTime = clamp(time, span.first, span.last) + span.lead;
  }

  // Moves one end of the stretch, and the player with it, so the picture
  // shows what the stretch now starts or ends on. The ends keep to the
  // footage and stay the shortest stretch apart.
  function moveTrim(dialog, which, time) {
    const span = trimSpan(dialog);
    const least = Number(dialog.dataset.min);
    let { from, to } = trimAt(dialog);
    if (which === "from") from = Math.max(span.first, Math.min(time, to - least));
    else to = Math.min(span.last, Math.max(time, from + least));
    dialog.dataset.from = from.toFixed(2);
    dialog.dataset.to = to.toFixed(2);
    const video = trimClip(dialog);
    video.pause();
    video.currentTime = (which === "from" ? from : to) + span.lead;
    paintTrim(dialog);
  }

  // Brings the dialog's strip, its readouts and its chat in line with the
  // stretch that is set and with the player.
  function paintTrim(dialog) {
    const span = trimSpan(dialog);
    const video = trimClip(dialog);
    const time = trimTime(dialog);
    let { from, to } = trimAt(dialog);
    // The file can turn out a little shorter than it was said to be.
    if (to > span.last) {
      to = span.last;
      dialog.dataset.to = to.toFixed(2);
    }

    const share = (at) => `${clamp((at - span.start) / (span.end - span.start), 0, 1) * 100}%`;
    const strip = one("[data-trim-strip]", dialog);
    strip.style.setProperty("--from", share(from));
    strip.style.setProperty("--to", share(to));
    strip.style.setProperty("--at", share(time));
    for (const [which, at, least, most] of [["from", from, span.first, to], ["to", to, from, span.last]]) {
      const handle = one(`[data-trim-handle="${which}"]`, strip);
      handle.setAttribute("aria-valuemin", least.toFixed(1));
      handle.setAttribute("aria-valuemax", most.toFixed(1));
      handle.setAttribute("aria-valuenow", at.toFixed(1));
      handle.setAttribute("aria-valuetext", trimWords(at, span.clip));
    }
    one("[data-trim-from]", dialog).textContent = trimWords(from, span.clip);
    one("[data-trim-to]", dialog).textContent = trimWords(to, span.clip);
    one("[data-trim-length]", dialog).textContent = `${(to - from).toFixed(1)} s`;
    one("[data-trim-now]", dialog).textContent = trimWords(time, span.clip);

    const playing = !video.paused && !video.ended;
    const play = one("[data-trim-play]", dialog);
    play.classList.toggle("is-playing", playing);
    play.setAttribute("aria-label", playing ? "Pause" : "Play");

    // Chat as its video will have it: each line where it stands on the
    // review page, moved by what the slider says.
    const pane = one("[data-trim-chat-pane]", dialog);
    if (pane && !pane.hidden) reveal(one("[data-trim-lines]", pane), time - Number(one("[data-trim-shift]", dialog).value));
  }

  // Played, the stretch stops at its end.
  function trimTick(video) {
    const dialog = trimOf(video);
    const time = trimTime(dialog);
    const { to } = trimAt(dialog);
    const ranPast = trimWas < to && time >= to && time - to < TRIM_RAN_PAST_SECONDS;
    trimWas = time;
    if (ranPast && !video.paused) {
      video.pause();
      video.currentTime = to + (Number(dialog.dataset.lead) || 0);
      trimWas = to;
    }
    paintTrim(dialog);
  }

  // Plays from where the player is - which can be outside the stretch, to
  // see what leads up to it or follows. From its end it starts over.
  function toggleTrimPlay(dialog) {
    const video = trimClip(dialog);
    if (!video.paused && !video.ended) {
      video.pause();
      return;
    }
    const { from, to } = trimAt(dialog);
    if (video.ended || Math.abs(trimTime(dialog) - to) < 0.05) {
      video.currentTime = from + (Number(dialog.dataset.lead) || 0);
      trimWas = from;
    }
    video.play().catch(() => {});
  }

  // Arrow keys nudge the handle that has the focus; I and O put the start
  // and the end where the player is; Space plays.
  const TRIM_KEYS = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1 };

  function trimKey(dialog, event) {
    const target = event.target;
    const handle = target.closest("[data-trim-handle]");
    if (handle && event.key in TRIM_KEYS) {
      event.preventDefault();
      const which = handle.dataset.trimHandle;
      moveTrim(dialog, which, trimAt(dialog)[which] + TRIM_KEYS[event.key] * (event.shiftKey ? 1 : 0.1));
      return;
    }
    if (event.repeat || target.matches("input")) return;
    const key = event.key.toLowerCase();
    if (key === "i") moveTrim(dialog, "from", trimTime(dialog));
    else if (key === "o") moveTrim(dialog, "to", trimTime(dialog));
    else if (key === " " && !target.matches("button, a")) {
      event.preventDefault();
      toggleTrimPlay(dialog);
    }
  }

  function openTrim(button) {
    const article = button.closest(".moment");
    const dialog = one("[data-trim]", article);
    const strip = one("[data-strip]", article);
    if (!dialog || !strip) return;
    one("[data-clip]", article).pause();

    const video = trimClip(dialog);
    if (!video.getAttribute("src")) {
      video.preload = "auto";
      video.src = video.dataset.src;
    }
    video.volume = volume;
    video.muted = muted;

    // The strip and the chat in the dialog are copies of the ones on the
    // page, taken now: where chat sits may have been moved since last time.
    // The strip's note on the moment is left out - a handle would cut it.
    const copies = (parts) => parts.map((part) => part.cloneNode(true));
    const drawn = all(":scope > *", strip).filter((part) => !part.matches("[data-hover], [data-playhead], [data-flag], .ch-note"));
    one("[data-trim-drawing]", dialog).replaceChildren(...copies(drawn));
    one("[data-trim-axis]", dialog).replaceChildren(...copies(all("[data-trace] .ch-axis > *", article)));
    one("[data-trim-lines]", dialog)?.replaceChildren(...copies(all("[data-chat] > [data-at]", article)));

    dialog.showModal();
    // To begin with the stretch is the clip, as it was cut.
    const span = trimSpan(dialog);
    if (dialog.dataset.from === undefined) {
      dialog.dataset.from = "0";
      dialog.dataset.to = Math.min(span.clip, span.last).toFixed(2);
    }
    trimWas = Number(dialog.dataset.from);
    video.currentTime = trimWas + span.lead;
    paintTrim(dialog);
    askTrim(dialog);
  }

  function leaveTrim(dialog) {
    trimClip(dialog).pause();
    clearTimeout(trimWatch);
  }

  // Whether the chat goes with the video: its lines are then shown beside
  // the picture, and can be moved against it.
  function wantTrimChat(box) {
    const dialog = trimOf(box);
    one("[data-trim-chat-pane]", dialog).hidden = !box.checked;
    one("[data-trim-shift]", dialog).disabled = !box.checked;
    paintTrim(dialog);
  }

  function shiftTrimChat(slider) {
    const dialog = trimOf(slider);
    const seconds = Number(slider.value);
    one("[data-trim-shift-words]", dialog).textContent = seconds
      ? `Chat ${Math.abs(seconds)} s ${seconds > 0 ? "later" : "earlier"}`
      : "Chat where the review page has it";
    paintTrim(dialog);
  }

  // Says where the cut stands and offers what there is to download.
  function showTrim(dialog, trim) {
    const busy = TRIM_BUSY.includes(trim.state);
    one("[data-trim-state]", dialog).textContent = trim.words;
    for (const [part, url] of [["[data-trim-video]", trim.video_url], ["[data-trim-chat-video]", trim.chat_url]]) {
      const link = one(part, dialog);
      link.hidden = !url;
      if (url) link.href = url;
      else link.removeAttribute("href");
    }
    one("[data-trim-go]", dialog).disabled = busy;
    clearTimeout(trimWatch);
    if (busy) trimWatch = setTimeout(() => askTrim(dialog), TRIM_POLL_MS);
  }

  async function askTrim(dialog) {
    const still = () => dialog.isConnected && dialog.open;
    if (!still()) return;
    let trim = null;
    try {
      const response = await fetch(`/moments/${dialog.closest(".moment").dataset.moment}/trim`);
      if (response.ok) trim = await response.json();
    } catch {
      // not reachable just now
    }
    if (!still()) return;
    if (trim) showTrim(dialog, trim);
    else if (one("[data-trim-go]", dialog).disabled) {
      // One is being made: asked again.
      clearTimeout(trimWatch);
      trimWatch = setTimeout(() => askTrim(dialog), TRIM_POLL_MS);
    }
  }

  async function exportTrim(button) {
    const dialog = trimOf(button);
    const { from, to } = trimAt(dialog);
    const chat = one("[data-trim-chat]", dialog);
    const shift = one("[data-trim-shift]", dialog);
    button.disabled = true;
    const response = await post(`/moments/${dialog.closest(".moment").dataset.moment}/trim`, {
      start: from,
      end: to,
      chat: Boolean(chat && chat.checked),
      chat_shift: shift ? Number(shift.value) : 0,
    });
    if (!dialog.isConnected) return; // another moment was opened meanwhile
    if (!response || !response.ok) {
      button.disabled = false;
      one("[data-trim-state]", dialog).textContent = "Could not start the cut. Try again.";
      return;
    }
    showTrim(dialog, await response.json());
  }

  // Fits what the script looks after to markup that has just arrived.
  function dress() {
    for (const field of all("[data-note]")) grow(field);
    for (const button of all("[data-trim-open]")) button.hidden = false;
    const video = one("[data-clip]");
    if (!video) return;
    video.defaultPlaybackRate = speed;
    video.playbackRate = speed;
    video.volume = volume;
    video.muted = muted;
    paint(video);
  }

  // ---- Switches, the watchlist, shutting down --------------------------

  // Pressing the half that is not lit sets the switch to it.
  async function flip(half) {
    if (half.getAttribute("aria-pressed") === "true") return;
    const group = half.closest("[data-switch]");
    const response = await post(`${group.dataset.switch}?enabled=${half.dataset.enabled}`);
    if (!response || !response.ok) {
      say(group, "Could not save the change. Try again.");
      return;
    }
    say(group, "");
    for (const other of all("button", group)) other.setAttribute("aria-pressed", String(other === half));
  }

  async function addChannel(form) {
    const name = form.elements.slug.value.trim();
    if (!name) return;
    say(form, `Adding ${name}.`);
    const response = await post(`/channels?slug=${encodeURIComponent(name)}`);
    if (response && response.ok) {
      location.reload();
    } else if (response && response.status === 404) {
      say(form, `No channel called ${name} on Kick. Check the spelling.`);
    } else {
      say(form, `Could not add ${name}. The server log says why.`);
    }
  }

  // Shutting down is asked for twice: the button, then the same word again
  // next to a sentence that says what it will do.
  function askToShutDown(asking) {
    const section = one("[data-shutdown]");
    one("[data-shutdown-ask]", section).hidden = asking;
    one("[data-shutdown-confirm]", section).hidden = !asking;
    // Cancel takes the focus, so a stray Enter or Space backs out.
    one(asking ? "[data-shutdown-cancel]" : "[data-shutdown-open]", section).focus();
  }

  async function shutDown(button) {
    const section = button.closest("[data-shutdown]");
    button.disabled = true;
    const response = await post("/shutdown");
    if (!response || !response.ok) {
      button.disabled = false;
      say(section, "Could not reach the service. It may have stopped already.");
      return;
    }
    const pending = (await response.json()).pending_work_count;
    one("[data-shutdown-confirm]", section).hidden = true;
    say(
      section,
      pending
        ? `Shutting down. Waiting for ${plural(pending, "job")} to finish (clips being cut or analysed).`
        : "Shutting down. This page stops answering once the service has exited.",
    );
  }

  // ---- Wiring -----------------------------------------------------------

  const onClick = [
    ["a.ch-row", (row, event) => isPlainClick(event) && (event.preventDefault(), openMoment(row, { play: true }))],
    ["[data-rate]", (key) => rate(key.closest(".moment"), Number(key.dataset.rate))],
    ["[data-tag]", setTag],
    ["[data-chat-shift]", shiftChat],
    ["[data-trim-open]", openTrim],
    ["[data-trim-close]", (button) => trimOf(button).close()],
    ["[data-trim-play]", (button) => toggleTrimPlay(trimOf(button))],
    ["[data-trim-clip]", (video) => toggleTrimPlay(trimOf(video))],
    ["[data-trim-go]", exportTrim],
    ["[data-switch] button", flip],
    ["[data-play]", (button) => togglePlay(clipOf(button))],
    ["[data-speed]", (button) => setSpeed(clipOf(button))],
    ["[data-mute]", (button) => toggleMute(clipOf(button))],
    ["[data-fullscreen]", (button) => toggleFullScreen(clipOf(button))],
    // In full screen the browser's own controls are showing and already do this.
    ["[data-clip]", (video) => video.controls || togglePlay(video)],
    ["[data-shutdown-open]", () => askToShutDown(true)],
    ["[data-shutdown-cancel]", () => askToShutDown(false)],
    ["[data-shutdown-go]", shutDown],
  ];

  document.addEventListener("click", (event) => {
    for (const [selector, handle] of onClick) {
      const element = event.target.closest(selector);
      if (!element) continue;
      handle(element, event);
      // A control pressed with the mouse does not keep the focus, so Space
      // goes back to playing the clip instead of pressing the control again.
      if (event.detail > 0 && element.matches("a, button")) element.blur();
      return;
    }
  });

  // Pressing on the strip goes there, and so does dragging along it; a
  // pointer that is only passing over it gets a readout instead.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (!strip || event.button !== 0) return;
    strip.setPointerCapture(event.pointerId);
    goTo(clipOf(strip), pointOn(strip, event).time);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (!strip) return;
    if (strip.hasPointerCapture(event.pointerId)) goTo(clipOf(strip), pointOn(strip, event).time);
    readOut(strip, event);
  });

  document.addEventListener("pointerout", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (strip && !strip.contains(event.relatedTarget)) stopReadOut(strip);
  });

  // In the Trim dialog: pressing a handle takes hold of it, pressing the
  // strip anywhere else goes there, and dragging does either along it.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip || event.button !== 0) return;
    const dialog = trimOf(strip);
    const handle = event.target.closest("[data-trim-handle]");
    const time = trimPoint(strip, event);
    strip.setPointerCapture(event.pointerId);
    trimDrag = handle ? handle.dataset.trimHandle : null;
    // A handle is as wide as a finger, and is held wherever it was pressed.
    if (trimDrag) trimGrab = time - trimAt(dialog)[trimDrag];
    else seekTrim(dialog, time);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip || !strip.hasPointerCapture(event.pointerId)) return;
    const time = trimPoint(strip, event);
    if (trimDrag) moveTrim(trimOf(strip), trimDrag, time - trimGrab);
    else seekTrim(trimOf(strip), time);
  });

  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      trimDrag = null;
    });
  }

  document.addEventListener("dblclick", (event) => {
    const video = event.target.closest("[data-clip]");
    if (video) toggleFullScreen(video);
    // Chat's slider rests in the middle, and goes back there when asked.
    const shift = event.target.closest("[data-trim-shift]");
    if (shift && !shift.disabled) {
      shift.value = "0";
      shiftTrimChat(shift);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    // With the Trim dialog open the keys are its own: a number must not
    // rate the moment underneath.
    const dialog = one("[data-trim][open]");
    if (dialog) {
      trimKey(dialog, event);
      return;
    }
    if (target.matches("[data-title]")) {
      // Enter finishes the name and so does Escape; leaving the field saves it.
      if (event.key === "Escape" || event.key === "Enter") {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (target.matches("[data-note]")) {
      // Enter finishes the note (Shift+Enter starts a new line in it), and
      // so does Escape; leaving the field is what saves it.
      if (event.key === "Escape" || (event.key === "Enter" && !event.shiftKey)) {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (target.matches("[data-strip]") && stripKey(target, event)) return;
    if (event.repeat || target.matches("input, textarea, select") || !one(".review")) return;

    const key = event.key.toLowerCase();
    if (key === "j") move(1);
    else if (key === "k") move(-1);
    else if (key.length === 1 && key >= "1" && key <= "5") rateFromKeyboard(Number(key));
    else if (key === " " && !target.matches("button, a.ch-btn, a.ch-tab")) {
      event.preventDefault();
      togglePlay(one("[data-clip]"));
    }
  });

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target.matches("[data-note]")) {
      grow(target);
      say(target, "");
    } else if (target.matches("[data-volume]")) {
      setVolume(clipOf(target), Number(target.value));
    } else if (target.matches("[data-trim-shift]")) {
      shiftTrimChat(target);
    }
  });

  // A slider let go of with the mouse does not keep the focus, so Space goes
  // back to playing the clip.
  document.addEventListener("pointerup", (event) => {
    if (event.target.matches?.("[data-volume]")) event.target.blur();
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-note]")) saveNote(target);
    else if (target.matches("[data-title]")) saveTitle(target);
    else if (target.matches("[data-trim-chat]")) wantTrimChat(target);
    else if (target.matches("[data-submit-on-change]")) target.form.requestSubmit();
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-add-channel]");
    if (!form) return;
    event.preventDefault();
    addChannel(form);
  });

  // Media events do not bubble, so they are caught on the way down - and
  // neither does a dialog's closing.
  const onMedia = (selector, handle) => {
    for (const type of ["loadedmetadata", "durationchange", "timeupdate", "seeked", "play", "pause", "ended", "emptied", "volumechange", "ratechange"]) {
      document.addEventListener(type, (event) => event.target.matches?.(selector) && handle(event.target), true);
    }
  };
  onMedia("[data-clip]", paint);
  onMedia("[data-trim-clip]", trimTick);
  document.addEventListener("close", (event) => event.target.matches?.("[data-trim]") && leaveTrim(event.target), true);

  // The page's own transport is not on screen in full screen, so the
  // browser's controls stand in for it for as long as that lasts.
  document.addEventListener("fullscreenchange", () => {
    for (const video of all("[data-clip]")) video.controls = document.fullscreenElement === video;
  });

  window.addEventListener("pagehide", () => {
    saveNote();
    saveTitle();
  });

  dress();
  currentRow()?.scrollIntoView({ block: "nearest" });
  if (one("[data-news]")) setInterval(checkForNew, NEWS_INTERVAL_MS);
})();
