"""Package registration for the local TypeScript reconstruction workspace."""

import streamlit as st

replay_component = st.components.v2.component(
    "minivision-reconstruct.replay", js="workspace.js", css="workspace.css", html="<div class='replay-root'></div>",
)
