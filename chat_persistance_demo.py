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
)

from langgraph.graph import (
    StateGraph,
    START,
    END,
)

from langgraph.graph.message import add_messages

from langgraph.checkpoint.sqlite import SqliteSaver


# =========================================================
# Environment
# =========================================================

load_dotenv()


# =========================================================
# Page Configuration
# =========================================================

st.set_page_config(
    page_title="AI Chatbot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =========================================================
# Custom CSS
# =========================================================

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
# DATABASE
# =========================================================

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
# LangGraph Checkpointer
# =========================================================

checkpoint_conn = sqlite3.connect(
    "checkpoints.db",
    check_same_thread=False,
)


checkpointer = SqliteSaver(
    checkpoint_conn
)


# =========================================================
# LangGraph State
# =========================================================

class State(TypedDict):

    messages: Annotated[
        list[BaseMessage],
        add_messages,
    ]


# =========================================================
# LLM
# =========================================================

llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
)


def log_tokens(ai_message):
     # =============================================
            # Token Usage — per turn
            # =============================================

            usage = getattr(ai_message, "usage_metadata", None)

            if usage:

                input_tok = usage.get("input_tokens", 0)
                output_tok = usage.get("output_tokens", 0)
                total_tok = usage.get("total_tokens", 0)

                # cache_read = (
                #     usage.get("input_token_details", {})
                #     .get("cache_read", 0)
                # )

                # reasoning_tok = (
                #     usage.get("output_token_details", {})
                #     .get("reasoning", 0)
                # )

                st.caption(
                    f"🔢 Tokens — Input: `{input_tok}` | "
                    f"Output: `{output_tok}` | "
                    f"Total: `{total_tok}` | "
                   # f"Cached: `{cache_read}` | "
                   # f"Reasoning: `{reasoning_tok}`"
                )
# =========================================================
# Chatbot Node
# =========================================================

def chatbot(state: State) -> State:

    response = llm.invoke(
        state["messages"]
    )


    log_tokens(response)
    return {
        "messages": [
            response
        ]
    }


# =========================================================
# Build Graph
# =========================================================

builder = StateGraph(State)


builder.add_node(
    "chatbot",
    chatbot,
)


builder.add_edge(
    START,
    "chatbot",
)


builder.add_edge(
    "chatbot",
    END,
)


# =========================================================
# Compile Graph
# =========================================================

graph = builder.compile(
    checkpointer=checkpointer,
)


# =========================================================
# DATABASE FUNCTIONS
# =========================================================

def load_chats():
    """
    Load all chats from SQLite.
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


# =========================================================

def create_chat_in_db(title="New Chat"):
    """
    Create a new chat in SQLite.
    """

    chat_id = str(
        uuid.uuid4()
    )

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


# =========================================================

def update_chat_title(
    chat_id: str,
    title: str,
):
    """
    Update chat title.
    """

    db_conn.execute(
        """
        UPDATE chats
        SET
            title = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = ?
        """,
        (
            title,
            chat_id,
        ),
    )

    db_conn.commit()


# =========================================================

def update_chat_timestamp(
    chat_id: str,
):
    """
    Update chat's last activity time.
    """

    db_conn.execute(
        """
        UPDATE chats
        SET
            updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = ?
        """,
        (
            chat_id,
        ),
    )

    db_conn.commit()


# =========================================================

def delete_chat_from_db(
    chat_id: str,
):
    """
    Delete chat metadata.

    LangGraph checkpoint cleanup is handled separately.
    """

    db_conn.execute(
        """
        DELETE FROM chats
        WHERE chat_id = ?
        """,
        (
            chat_id,
        ),
    )

    db_conn.commit()


# =========================================================

def get_chat_title(
    chat_id: str,
):
    """
    Get chat title from database.
    """

    result = db_conn.execute(
        """
        SELECT title
        FROM chats
        WHERE chat_id = ?
        """,
        (
            chat_id,
        ),
    ).fetchone()

    if result:
        return result[0]

    return "New Chat"


# =========================================================
# SESSION STATE
# =========================================================

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
# Delete Chat
# =========================================================

def delete_chat(
    chat_id: str,
):

    delete_chat_from_db(
        chat_id
    )

    # Reload chats

    st.session_state.chats = load_chats()

    # If chats still exist

    if st.session_state.chats:

        st.session_state.current_chat = next(
            iter(st.session_state.chats)
        )

    else:

        # Always maintain at least one chat

        new_chat_id = create_chat_in_db(
            "New Chat"
        )

        st.session_state.chats = load_chats()

        st.session_state.current_chat = new_chat_id


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

        col1, col2 = st.columns(
            [5, 1]
        )


        with col1:

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

                st.session_state.current_chat = (
                    chat_id
                )

                st.rerun()


        with col2:

            if st.button(
                "⋮",
                key=f"menu_{chat_id}",
                use_container_width=True,
            ):

                st.session_state[
                    "menu_chat"
                ] = chat_id

                st.rerun()


    # =====================================================
    # Chat Menu
    # =====================================================

    if "menu_chat" in st.session_state:

        menu_chat_id = (
            st.session_state["menu_chat"]
        )

        if menu_chat_id in st.session_state.chats:

            menu_chat = (
                st.session_state.chats[
                    menu_chat_id
                ]
            )

            st.divider()

            st.markdown(
                f"**{menu_chat['title']}**"
            )


            # -------------------------------------------------
            # Rename
            # -------------------------------------------------

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

                    update_chat_title(
                        menu_chat_id,
                        new_title,
                    )

                    st.session_state.chats = (
                        load_chats()
                    )

                    del st.session_state[
                        "menu_chat"
                    ]

                    st.rerun()


            # -------------------------------------------------
            # Delete
            # -------------------------------------------------

            if st.button(
                "🗑 Delete Chat",
                key=f"delete_{menu_chat_id}",
                use_container_width=True,
            ):

                delete_chat(
                    menu_chat_id
                )

                del st.session_state[
                    "menu_chat"
                ]

                st.rerun()


            # -------------------------------------------------
            # Close Menu
            # -------------------------------------------------

            if st.button(
                "Cancel",
                key=f"cancel_{menu_chat_id}",
                use_container_width=True,
            ):

                del st.session_state[
                    "menu_chat"
                ]

                st.rerun()


    st.divider()


    st.caption(
        "Chats are permanently stored in SQLite."
    )


# =========================================================
# Current Chat
# =========================================================

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
# Graph Configuration
# =========================================================

config = {

    "configurable": {

        "thread_id": thread_id

    }

}


# =========================================================
# Main Chat Area
# =========================================================

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
# Load Conversation From LangGraph
# =========================================================

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
# Display Messages
# =========================================================

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
# Empty State
# =========================================================

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
# Chat Input
# =========================================================

user_message = st.chat_input(
    "Message AI Chatbot..."
)


# =========================================================
# Process User Message
# =========================================================

if user_message:

    user_message = user_message.strip()


    if not user_message:

        st.stop()


    # =====================================================
    # Update Chat Title
    # =====================================================

    if current_chat["title"] == "New Chat":

        title = user_message[:30]

        if len(user_message) > 30:

            title += "..."


        update_chat_title(
            current_chat_id,
            title,
        )


        current_chat["title"] = title


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
# Footer
# =========================================================

st.markdown(
    """
    <div class="app-footer">
        Powered by LangGraph + OpenAI + SQLite
    </div>
    """,
    unsafe_allow_html=True,
)