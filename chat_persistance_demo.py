"""
AI Chatbot using Streamlit + LangGraph + SQLite.

Architecture:
1. SQLite stores chat metadata.
2. LangGraph SqliteSaver persists conversation state/checkpoints.
3. `messages` keeps the complete conversation history.
4. `summary` stores compressed long-term conversation memory.
5. `trim_messages` limits the recent context sent to the LLM.
6. Chatbot generates the answer using summary + recent messages.
7. Summarizer periodically refreshes the compressed memory.

The important token-optimization idea is:
    Full history for persistence
            ↓
    Summary + recent messages
            ↓
           LLM
"""

import uuid
import sqlite3
from datetime import datetime

import streamlit as st

from typing import TypedDict, Annotated

from dotenv import load_dotenv

from langchain_openai import ChatOpenAI

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    AIMessage,
    SystemMessage,
)
from langchain_core.messages.utils import trim_messages

from langgraph.graph import (
    StateGraph,
    START,
    END,
)

from langgraph.graph.message import add_messages

from langgraph.checkpoint.sqlite import SqliteSaver


# =========================================================
# ENVIRONMENT
# =========================================================
# Load variables such as OPENAI_API_KEY from the .env file.
# Keeping secrets in environment variables prevents credentials
# from being hard-coded directly into the application.

load_dotenv()


# =========================================================
# STREAMLIT PAGE CONFIGURATION
# =========================================================
# Configure the browser page title, icon, layout, and initial
# sidebar state before rendering the rest of the application.

st.set_page_config(
    page_title="AI Chatbot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =========================================================
# CUSTOM CSS
# =========================================================
# Keep presentation-related code separate from application logic.
# This CSS controls the sidebar, chat messages, title, and footer.

st.markdown(
    """
    <style>

    /* Main container */

    .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
    }


    /* Sidebar */

    section[data-testid="stSidebar"] {
        width: 280px !important;
    }


    /* Chat title */

    .chat-title {
        font-size: 24px;
        font-weight: 700;
        margin-bottom: 2px;
    }


    .chat-subtitle {
        color: #777;
        font-size: 14px;
        margin-bottom: 20px;
    }


    /* Sidebar chat buttons */

    section[data-testid="stSidebar"] .stButton > button {
        text-align: left;
        border: none;
        background: transparent;
        padding: 8px 10px;
        border-radius: 8px;
    }


    section[data-testid="stSidebar"] .stButton > button:hover {
        background-color: rgba(128, 128, 128, 0.15);
    }


    /* New chat button */

    section[data-testid="stSidebar"] button[kind="primary"] {
        width: 100%;
    }


    /* Chat message */

    [data-testid="stChatMessage"] {
        padding-top: 0.5rem;
        padding-bottom: 0.5rem;
    }


    /* Footer */

    .app-footer {
        text-align: center;
        color: #888;
        font-size: 12px;
        margin-top: 30px;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# APPLICATION DATABASE
# =========================================================
# This SQLite database stores lightweight chat metadata such as
# chat_id, title, and timestamps.
#
# It is intentionally separate from the LangGraph checkpoint DB.
# `app.db` answers: "Which chats exist?"
# `checkpoints.db` answers: "What is the conversation state?"

# ---------------------------------------------------------
# Application Database
#
# Stores:
#   - chat_id
#   - title
#   - created_at
#   - updated_at
# ---------------------------------------------------------

db_conn = sqlite3.connect(
    "app.db",
    check_same_thread=False,
)


db_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
)

db_conn.commit()


# =========================================================
# LANGGRAPH CHECKPOINTER
# =========================================================
# SqliteSaver persists LangGraph state between Streamlit reruns.
# The thread_id identifies one conversation, allowing the graph
# to restore the correct state when the user switches chats.

checkpoint_conn = sqlite3.connect(
    "checkpoints.db",
    check_same_thread=False,
)


checkpointer = SqliteSaver(
    checkpoint_conn
)


# =========================================================
# LANGGRAPH STATE
# =========================================================
# State is the shared data passed between LangGraph nodes.
#
# messages:
#   Complete conversation history. This is retained for persistence
#   and UI display.
#
# summary:
#   Compressed memory of older conversation context. This is used
#   to give the LLM the important historical context without sending
#   the entire conversation every time.

class State(TypedDict):

    messages: Annotated[
        list[BaseMessage],
        add_messages,
    ]

    # Compressed memory of older conversation turns.
    # Unlike messages, this value is overwritten with the latest summary.
    summary: str


# =========================================================
# LLM
# =========================================================
# Create one reusable chat-model instance.
# temperature=0 keeps responses deterministic and is also useful
# for the summarization step because we want stable factual memory.

llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
)


def log_tokens(ai_message):
    """
    Token Usage — per turn
    """

    usage = getattr(ai_message, "usage_metadata", None)
    if not usage:
        return

    input_tok = usage.get("input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    total_tok = usage.get("total_tokens", 0)

    st.caption(
        f"🔢 Tokens — Input: `{input_tok}` | "
        f"Output: `{output_tok}` | "
        f"Total: `{total_tok}`"
    )
# =========================================================
# CHATBOT NODE
# =========================================================
# This is the main generation node.
#
# Token optimization happens here:
#   1. Read the compressed summary.
#   2. Trim the conversation to recent messages.
#   3. Send summary + recent messages to the LLM.
#
# We do NOT send the complete checkpoint history to the LLM.

# Keep only a small recent window in the actual LLM prompt.
# The complete conversation is still retained by LangGraph/checkpointer
# and remains available to the UI.
MAX_RECENT_MESSAGES = 3  # Kept as a documented tuning knob for recent context size.


def chatbot(state: State) -> State:

    summary = state.get("summary", "").strip()

    # Trim from the end so the model sees the newest conversation turns.
    # This prevents the prompt from growing linearly with total history.

    last_messages = state["messages"][-MAX_RECENT_MESSAGES:]

    recent_messages = trim_messages(
        last_messages,
        max_tokens=2500,
        strategy="last",
        token_counter=llm,
        include_system=True,
        allow_partial=False,
    )

    # Build the actual prompt context independently from the persisted state.
    # This is the key distinction:
    #   persisted state = full history
    #   LLM context     = summary + recent messages
    context_messages = []

    if summary:
        context_messages.append(
            SystemMessage(
                content=(
                    "Here is a compressed summary of the earlier conversation. "
                    "Use it as memory when answering the user.\\n\\n"
                    f"Conversation summary:\\n{summary}"
                )
            )
        )

        print(f"{summary}")

    # Recent messages provide short-term context while the summary
    # provides long-term context from older parts of the conversation.
    context_messages.extend(recent_messages)

    print(f"Context Messages:{context_messages}")

    response = llm.invoke(context_messages)

    log_tokens(response)

    return {
        "messages": [
            response
        ]
    }


# =========================================================
# SUMMARIZER NODE
# =========================================================
# This node converts a long conversation into a compact memory.
#
# The full messages remain in LangGraph state, but the summary
# captures the important information that older messages contain.
# This lets future chatbot calls use the summary instead of replaying
# the complete conversation history.

SUMMARY_TRIGGER_MESSAGES = 3


def summarizer(state: State) -> State:
    """
    Compress older conversation context into one summary.

    We keep the full messages in LangGraph for history/UI, but the chatbot
    does not need to send all of them to the model on every turn.
    """

    messages = state["messages"]

    # Do not make an additional LLM call until the conversation is large
    # enough to benefit from summarization. This avoids paying for a
    # summary-generation request on every small conversation.
    if len(messages) < SUMMARY_TRIGGER_MESSAGES:
        return {
            "summary": state.get("summary", "")
        }

    old_summary = state.get("summary", "").strip()

    conversation_text = "\n".join(
        f"{type(message).__name__}: {message.content}"
        for message in messages
        if message.content
    )

    summary_prompt = f"""
Create a compact, factual memory of this conversation.

Preserve:
- user goals and requirements
- important decisions
- technical details
- preferences/constraints mentioned by the user
- unresolved questions or pending work

Remove:
- greetings
- repetition
- unnecessary wording
- verbose explanations

Previous summary:
{old_summary or "(none)"}

Conversation:
{conversation_text}

Return only the updated summary.
""".strip()

    # This is a separate LLM call whose only job is memory compression.
    # Its output becomes the latest value of `summary`.
    summary_response = llm.invoke(
        [HumanMessage(content=summary_prompt)]
    )

    # Returning only `summary` updates the compressed memory while
    # leaving the complete `messages` history available in state.
    return {
        "summary": summary_response.content.strip()
    }


# =========================================================
# BUILD LANGGRAPH
# =========================================================
# The graph represents the execution pipeline:
#
# START → chatbot → summarizer → END
#
# The chatbot answers the current request first.
# The summarizer then refreshes long-term memory when necessary.



# =========================================================
# BUILD LANGGRAPH
# =========================================================
# The graph represents the execution pipeline:
#
# START → chatbot → summarizer → END
#
# The chatbot answers the current request first.
# The summarizer then refreshes long-term memory when necessary.

builder = StateGraph(State)


builder.add_node(
    "chatbot",
    chatbot,
)


builder.add_node(
    "summarizer",
    summarizer,
)


builder.add_edge(
    START,
    "summarizer",
)


builder.add_edge(
    "summarizer",
    "chatbot",
)


builder.add_edge(
    "chatbot",
    END,
)


# =========================================================
# COMPILE GRAPH
# =========================================================
# Compile the StateGraph and attach the SQLite checkpointer.
# The checkpointer makes state persistent across application reruns.

graph = builder.compile(
    checkpointer=checkpointer,
)


# =========================================================
# DATABASE FUNCTIONS
# =========================================================
# These functions handle only application-level chat metadata.
# Conversation messages and summary are managed by LangGraph.

def load_chats():
    """
    Load all chat metadata from SQLite.

    This function does not load conversation messages. Those belong to
    LangGraph's checkpoint state and are restored using thread_id.
    """

    rows = db_conn.execute(
        """
        SELECT
            chat_id,
            title,
            created_at,
            updated_at
        FROM chats
        ORDER BY updated_at DESC
        """
    ).fetchall()

    chats = {}

    for row in rows:
        chat_id = row[0]

        chats[chat_id] = {
            "title": row[1],
            "thread_id": chat_id,
            "created_at": row[2],
            "updated_at": row[3],
        }

    return chats


def create_chat_in_db(title="New Chat"):
    """
    Create a new chat record and return its unique thread ID.

    The returned chat_id is also used as LangGraph's thread_id so the
    application database and LangGraph checkpoint refer to the same chat.
    """

    chat_id = str(uuid.uuid4())

    db_conn.execute(
        """
        INSERT INTO chats (
            chat_id,
            title
        )
        VALUES (?, ?)
        """,
        (
            chat_id,
            title,
        ),
    )

    db_conn.commit()

    return chat_id


def update_chat_timestamp(chat_id: str):
    """
    Update the last-activity timestamp.

    This keeps the sidebar ordering useful by moving the active
    conversation toward the top after each successful interaction.
    """

    db_conn.execute(
        """
        UPDATE chats
        SET updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = ?
        """,
        (
            chat_id,
        ),
    )

    db_conn.commit()


def update_chat_title(chat_id: str, new_title: str):
    """
    Update the chat title in the application database.
    """

    db_conn.execute(
        """
        UPDATE chats
        SET title = ?
        WHERE chat_id = ?
        """,
        (new_title, chat_id),
    )

    db_conn.commit()


def delete_chat(chat_id: str):
    """
    Delete a chat from the application database.
    """

    db_conn.execute(
        """
        DELETE FROM chats
        WHERE chat_id = ?
        """,
        (chat_id,),
    )

    db_conn.commit()


# =========================================================
# STREAMLIT SESSION STATE
# =========================================================
# Streamlit reruns the script after user interaction.
# session_state keeps track of which chats are loaded and which
# conversation is currently selected.

# ---------------------------------------------------------
# Load chats from SQLite
# ---------------------------------------------------------

if "chats" not in st.session_state:

    st.session_state.chats = load_chats()


# =========================================================
# Create Initial Chat
# =========================================================

if not st.session_state.chats:

    chat_id = create_chat_in_db(
        "New Chat"
    )

    st.session_state.chats = load_chats()

    st.session_state.current_chat = chat_id


# =========================================================
# Restore Current Chat
# =========================================================

elif "current_chat" not in st.session_state:

    # Select most recently updated chat

    st.session_state.current_chat = next(
        iter(st.session_state.chats)
    )


# =========================================================
# Create New Chat
# =========================================================

def create_new_chat():

    chat_id = create_chat_in_db(
        "New Chat"
    )

    # Reload from DB

    st.session_state.chats = load_chats()

    st.session_state.current_chat = chat_id


# =========================================================
# Sidebar
# =========================================================

with st.sidebar:

    # -----------------------------------------------------
    # Header
    # -----------------------------------------------------

    st.markdown(
        "## 🤖 AI Chatbot"
    )

    st.caption(
        "LangGraph + SQLite"
    )

    st.divider()


    # -----------------------------------------------------
    # New Chat
    # -----------------------------------------------------

    if st.button(
        "＋  New Chat",
        use_container_width=True,
        type="primary",
    ):

        create_new_chat()

        st.rerun()


    st.divider()


    # -----------------------------------------------------
    # Chat List Header
    # -----------------------------------------------------

    st.markdown(
        "**Your Chats**"
    )


    # -----------------------------------------------------
    # Chat List
    # -----------------------------------------------------

    for chat_id, chat in (
        st.session_state.chats.items()
    ):

        title = chat["title"]

        is_current = (
            chat_id
            == st.session_state.current_chat
        )


        # -------------------------------------------------
        # Chat row
        # -------------------------------------------------

        icon = (
            "🟢"
            if is_current
            else "💬"
        )

        if st.button(
            f"{icon} {title}",
            key=f"chat_{chat_id}",
            use_container_width=True,
        ):

            st.session_state.current_chat = chat_id

            st.rerun()


    st.divider()

    # Inline chat menu (rename / delete) when requested
    if "menu_chat" in st.session_state:
        menu_chat_id = st.session_state["menu_chat"]
        menu_chat = st.session_state.chats.get(menu_chat_id)

        if menu_chat:
            st.markdown(f"**{menu_chat['title']}**")

            # Rename
            new_title = st.text_input(
                "Rename chat",
                value=menu_chat["title"],
                key=f"rename_{menu_chat_id}",
            )

            if st.button(
                "Save Name",
                key=f"save_name_{menu_chat_id}",
                use_container_width=True,
            ):
                new_title = new_title.strip()
                if new_title:
                    update_chat_title(menu_chat_id, new_title)
                    st.session_state.chats = load_chats()
                    del st.session_state["menu_chat"]
                    st.rerun()

            # Delete
            if st.button(
                "🗑 Delete Chat",
                key=f"delete_{menu_chat_id}",
                use_container_width=True,
            ):
                delete_chat(menu_chat_id)
                del st.session_state["menu_chat"]
                st.rerun()

            # Close Menu
            if st.button(
                "Cancel",
                key=f"cancel_{menu_chat_id}",
                use_container_width=True,
            ):
                del st.session_state["menu_chat"]
                st.rerun()

    st.divider()

    st.caption("Chats are stored in SQLite.")


# =========================================================
# CURRENT CHAT
# =========================================================
# Resolve the currently selected chat and its LangGraph thread ID.

current_chat_id = (
    st.session_state.current_chat
)


# ---------------------------------------------------------
# Safety check
# ---------------------------------------------------------

if current_chat_id not in st.session_state.chats:

    st.session_state.chats = load_chats()

    if current_chat_id not in st.session_state.chats:

        current_chat_id = next(
            iter(st.session_state.chats)
        )

        st.session_state.current_chat = (
            current_chat_id
        )


current_chat = (
    st.session_state.chats[
        current_chat_id
    ]
)


thread_id = current_chat[
    "thread_id"
]


# =========================================================
# GRAPH CONFIGURATION
# =========================================================
# thread_id is the key LangGraph uses to isolate one conversation
# from another when using the SQLite checkpointer.

config = {

    "configurable": {

        "thread_id": thread_id

    }

}


# =========================================================
# MAIN CHAT AREA
# =========================================================
# Render the main chatbot heading and description.

st.markdown(
    '<div class="chat-title">AI Chatbot</div>',
    unsafe_allow_html=True,
)


st.markdown(
    '<div class="chat-subtitle">'
    'Ask anything'
    '</div>',
    unsafe_allow_html=True,
)


# =========================================================
# LOAD CONVERSATION FROM LANGGRAPH
# =========================================================
# Read the checkpoint for the selected thread.
# This allows the UI to display the existing conversation after
# switching chats or restarting Streamlit.

try:

    snapshot = graph.get_state(
        config
    )

except Exception as error:

    st.error(
        f"Unable to load conversation: {error}"
    )

    snapshot = None


# =========================================================
# DISPLAY CONVERSATION
# =========================================================
# Render the persisted HumanMessage and AIMessage objects.
# The summary is intentionally not displayed as a chat bubble because
# it is internal memory used by the LLM.

if snapshot and snapshot.values:

    messages = snapshot.values.get(
        "messages",
        [],
    )


    for message in messages:

        # -------------------------------------------------
        # Human Message
        # -------------------------------------------------

        if isinstance(
            message,
            HumanMessage,
        ):

            with st.chat_message(
                "user"
            ):

                st.markdown(
                    message.content
                )


        # -------------------------------------------------
        # AI Message
        # -------------------------------------------------

        elif isinstance(
            message,
            AIMessage,
        ):

            with st.chat_message(
                "assistant"
            ):

                st.markdown(
                    message.content
                )


# =========================================================
# EMPTY STATE
# =========================================================
# Show a friendly placeholder when the selected conversation
# does not contain any messages yet.

if not snapshot or not snapshot.values:

    st.markdown(
        """
        <div style="
            text-align:center;
            margin-top:120px;
            color:#888;
        ">

        <div style="font-size:50px;">
        🤖
        </div>

        <h3>How can I help you?</h3>

        <p>
        Start a conversation with the AI assistant.
        </p>

        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# CHAT INPUT
# =========================================================
# Collect the user's next message from Streamlit's chat input.

user_message = st.chat_input(
    "Message AI Chatbot..."
)


# =========================================================
# PROCESS USER MESSAGE
# =========================================================
# Add the user's message to the LangGraph conversation and execute
# the graph. The graph handles both answer generation and memory
# summarization.

if user_message:

    user_message = user_message.strip()


    if not user_message:

        st.stop()


    # =====================================================
    # Display User Message
    # =====================================================

    with st.chat_message(
        "user"
    ):

        st.markdown(
            user_message
        )


    # =====================================================
    # Invoke LangGraph
    # =====================================================

    try:

        # Send only the new user message into the graph.
        # LangGraph merges it into the existing checkpointed state.
        # The chatbot node then constructs its optimized LLM context.
        result = graph.invoke(

            {
                "messages": [

                    HumanMessage(
                        content=user_message
                    )

                ]
            },

            config=config,
        )


        # =================================================
        # Update Chat Timestamp
        # =================================================

        update_chat_timestamp(
            current_chat_id
        )


        # Reload chat metadata

        st.session_state.chats = (
            load_chats()
        )


        # =================================================
        # Display AI Response
        # =================================================

        ai_message = (
            result["messages"][-1]
        )


        with st.chat_message(
            "assistant"
        ):

            st.markdown(
                ai_message.content
            )


    except Exception as error:

        st.error(
            f"Something went wrong: {error}"
        )


# =========================================================
# FOOTER
# =========================================================
# Small application branding/footer shown below the chat.

st.markdown(
    """
    <div class="app-footer">
        Powered by LangGraph + OpenAI + SQLite
    </div>
    """,
    unsafe_allow_html=True,
)