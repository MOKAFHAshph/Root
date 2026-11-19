"""Compatibility renderers also work when a utility module is imported by itself."""
from bot import runtime
import re

def _default(template,values):
    return re.sub(r'\{v(\d+)\}',lambda m:str(values[int(m.group(1))]) if int(m.group(1))<len(values) else m.group(),template)

def render_bot_text(key,template,values):
    try:renderer=runtime.get('render_bot_text')
    except KeyError:renderer=None
    if renderer and renderer is not render_bot_text:return renderer(key,template,values)
    return _default(template,values)

def render_bot_button(key,template,values):
    try:renderer=runtime.get('render_bot_button')
    except KeyError:renderer=None
    if renderer and renderer is not render_bot_button:return renderer(key,template,values)
    return _default(template,values)
