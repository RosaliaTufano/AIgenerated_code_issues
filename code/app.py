import json
import re
import html
import streamlit as st


@st.cache_data(show_spinner=False)
def load_json_cached(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("File JSON expected")

    return data


def normalize_conversation_rounds(conversation: dict):
    turns = []
    round_list = conversation.get("rounds", []) or []

    for turn_idx, turn in enumerate(round_list):
        turns.append(
            {
                "turn_idx": turn_idx,
                "round_id": turn.get("round_id"),
                "prompt": turn.get("Prompt", "") or "",
                "answer": turn.get("Answer", "") or "",
                "list_of_code": turn.get("ListOfCode_Answer", []) or [],
                "is_relevant": bool(turn.get("is_relevant", False)),
                "relevant_sentences": turn.get("relevant_sentences", []) or [],
            }
        )
    return turns


def dedup_sentences(sentences):
    uniq, seen = [], set()
    for s in sentences or []:
        if not isinstance(s, str):
            continue
        ss = s.strip()
        if ss and ss.lower() not in seen:
            uniq.append(ss)
            seen.add(ss.lower())
    uniq.sort(key=len, reverse=True)
    return uniq


def highlight_relevant_sentences_htmlescaped(safe_html_text: str, sentences: list[str]) -> str:
    if not safe_html_text or not sentences:
        return safe_html_text

    for s in dedup_sentences(sentences):
        safe_s = html.escape(s)
        pattern = re.compile(re.escape(safe_s), flags=re.IGNORECASE)
        safe_html_text = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe_html_text)
    return safe_html_text


def render_text_box(text: str, highlight_sentences=None):
    safe = html.escape(text or "")
    if highlight_sentences:
        safe = highlight_relevant_sentences_htmlescaped(safe, highlight_sentences)
    safe = safe.replace("\n", "<br>")
    st.markdown(f"<div class='mybox'>{safe}</div>", unsafe_allow_html=True)


CODE_TOKEN_RE = re.compile(r"(\[\[CODE_\d+_\d+\]\])")


def render_answer_with_code(answer: str, list_of_code: list, highlight_sentences=None):
    code_map = {
        c.get("ReplaceString"): c
        for c in (list_of_code or [])
        if isinstance(c, dict) and c.get("ReplaceString")
    }

    parts = CODE_TOKEN_RE.split(answer or "")
    for p in parts:
        if not p:
            continue

        if CODE_TOKEN_RE.fullmatch(p):
            code_obj = code_map.get(p)
            if code_obj and code_obj.get("Content") is not None:
                lang = (code_obj.get("Type") or "").strip() or None
                content = code_obj.get("Content", "")

                if content.startswith("Python\n"):
                    content = content[len("Python\n"):]
                    if not lang:
                        lang = "python"
                elif content.startswith("Bash\n"):
                    content = content[len("Bash\n"):]
                    if not lang:
                        lang = "bash"

                st.code(content, language=lang)
            else:
                render_text_box(p, highlight_sentences=highlight_sentences)
        else:
            render_text_box(p, highlight_sentences=highlight_sentences)


def inject_base_css():
    st.markdown(
        """
        <style>
        [data-testid="stToolbar"] { display: none; }
        [data-testid="stHeader"] { display: none; }
        footer { display: none; }

        div[data-testid="collapsedControl"] {
            display: none !important;
            visibility: hidden !important;
        }

        .mybox {
            padding: 0.75rem;
            border-radius: 10px;
            white-space: normal;
            word-break: break-word;
            border: 1px solid #ddd;
            background: #fff;
        }

        mark {
            padding: 0.05rem 0.15rem;
            border-radius: 0.25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="JSON Conversation Viewer",
    layout="wide",
    initial_sidebar_state="expanded",
)

# inject_base_css()

if "json_path" not in st.session_state:
    st.session_state["json_path"] = ""
if "data" not in st.session_state:
    st.session_state["data"] = None
st.session_state.setdefault("conv_idx", 0)
st.session_state.setdefault("turn_idx", 0)

st.title("JSON Conversation Viewer")

with st.sidebar:
    st.header("Input")
    json_path = st.text_input("JSON path", value=st.session_state.get("json_path", ""))

    if st.button("Upload JSON"):
        st.session_state["json_path"] = json_path
        try:
            loaded = load_json_cached(json_path)
            st.session_state["data"] = loaded
            st.session_state["conv_idx"] = 0
            st.session_state["turn_idx"] = 0
            st.success(f"Loaded {len(loaded)} conversations")
        except Exception as e:
            st.error(f"Loading error: {e}")

    st.divider()

data = st.session_state.get("data")
if not isinstance(data, list):
    st.info("Write the JSON path on the left and click **Upload JSON**.")
    raise SystemExit

n_conversations = len(data)
if n_conversations == 0:
    st.warning("JSON is empty.")
    raise SystemExit

conv_idx = max(0, min(st.session_state["conv_idx"], n_conversations - 1))
st.session_state["conv_idx"] = conv_idx
conversation = data[conv_idx]

share_link = conversation.get("share_link", "—")
conversation_id = conversation.get("id", "—")

turns = normalize_conversation_rounds(conversation)
if not turns:
    st.warning("This conversation does not contain any rounds.")
    raise SystemExit

turn_idx = max(0, min(st.session_state["turn_idx"], len(turns) - 1))
st.session_state["turn_idx"] = turn_idx
turn = turns[turn_idx]

# Header
top1, top2, top3 = st.columns([3, 3, 8])
with top1:
    st.metric("Conversation", f"{conv_idx + 1} / {n_conversations}")
with top2:
    st.metric("Conversation ID", conversation_id)
with top3:
    st.write("**Share link:**", share_link)

# Navigation conversations
nav1, nav2, nav3 = st.columns([1, 1, 6])

def goto_conversation(index: int):
    st.session_state["conv_idx"] = index
    st.session_state["turn_idx"] = 0
    st.rerun()

with nav1:
    if st.button("⬅️ Prev", disabled=(conv_idx == 0)):
        goto_conversation(conv_idx - 1)

with nav2:
    if st.button("➡️ Next", disabled=(conv_idx >= n_conversations - 1)):
        goto_conversation(conv_idx + 1)

with nav3:
    options = list(range(n_conversations))
    selected_conv = st.selectbox(
        "Go to conversation",
        options=options,
        index=conv_idx,
        format_func=lambda i: str(data[i].get("id", f"conv_{i}")),
    )
    if selected_conv != conv_idx:
        goto_conversation(selected_conv)

# Sidebar round index
with st.sidebar:
    st.header("Rounds")

    round_labels = []
    for i, t in enumerate(turns):
        rel = (t["is_relevant"] and bool(t["relevant_sentences"]))
        flag = "⭐" if rel else " "
        preview = t["prompt"].strip().replace("\n", " ")
        preview = preview[:60] + ("…" if len(preview) > 60 else "")
        round_labels.append(f"{flag} Round {i+1}: {preview}")

    chosen_turn = st.radio(
        "Round",
        options=list(range(len(turns))),
        index=turn_idx,
        format_func=lambda idx: round_labels[idx],
        label_visibility="collapsed",
    )
    if chosen_turn != turn_idx:
        st.session_state["turn_idx"] = chosen_turn
        st.rerun()

    st.caption("⭐ = relevant round")

# Main
st.divider()
st.write(f"**Round {turn_idx + 1} / {len(turns)}**")

rnav1, rnav2, _ = st.columns([1, 1, 5])
with rnav1:
    if st.button("⏮️ Prev round", disabled=(turn_idx == 0)):
        st.session_state["turn_idx"] -= 1
        st.rerun()
with rnav2:
    if st.button("⏭️ Next round", disabled=(turn_idx >= len(turns) - 1)):
        st.session_state["turn_idx"] += 1
        st.rerun()

left, right = st.columns([1, 1])

with left:
    st.subheader("Prompt")
    render_text_box(turn["prompt"])

with right:
    st.subheader("Answer")
    do_highlight = bool(turn["is_relevant"]) and bool(turn["relevant_sentences"])
    highlight_sentences = turn["relevant_sentences"] if do_highlight else None

    if do_highlight:
        st.caption("Highlighting enabled (is_relevant=True)")

    render_answer_with_code(
        answer=turn["answer"],
        list_of_code=turn["list_of_code"],
        highlight_sentences=highlight_sentences,
    )

    if do_highlight:
        with st.expander("Relevant sentences"):
            for s in turn["relevant_sentences"]:
                st.write(f"- {s}")