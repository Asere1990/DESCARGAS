"""Presentation-only formatting for bot dialogs, never Mini App or media captions."""
import copy
import re
import unicodedata

_TOKENS = re.compile(r'https?://[^\s<>]+|tg://[^\s<>]+|[\w.+-]+@[\w.-]+|/[A-Za-z_][\w@]*|@[\w]+|&(?:#\d+|#x[\da-fA-F]+|\w+);|[^\W\d_]+', re.UNICODE)
_HTML = re.compile(r'(<[^>]*>|&(?:#\d+|#x[\da-fA-F]+|\w+);)')
_SERIF = range(0x1D400, 0x1D434)
_VERBATIM = {'url', 'email', 'mention', 'bot_command', 'code', 'pre', 'phone_number'}


def _letter(char):
    # Restore sans mathematical alphabets only; retain all serif text verbatim.
    codepoint = ord(char)
    if 0x1D5A0 <= codepoint <= 0x1D66F or 0x1D7E2 <= codepoint <= 0x1D7FF:
        return unicodedata.normalize('NFKC', char)
    return char


def _plain(text, entities=()):
    # Telegram entity offsets use UTF-16, including the original serif letters.
    spans = [(e['offset'], e['offset'] + e['length']) for e in entities if e.get('type') in _VERBATIM]
    protected = set()
    for match in _TOKENS.finditer(text):
        word = match.group()
        if word.startswith(('http://', 'https://', 'tg://', '/', '@', '&')) or '@' in word or any(ord(c) in _SERIF for c in word):
            protected.update(range(match.start(), match.end()))
    result, offsets, old, new = [], {0: 0}, 0, 0
    for index, char in enumerate(text):
        width = len(char.encode('utf-16-le')) // 2
        keep = index in protected or any(a <= old < b for a, b in spans)
        converted = char if keep else _letter(char)
        result.append(converted)
        new_width = len(converted.encode('utf-16-le')) // 2
        for unit in range(1, width):
            offsets[old + unit] = new + min(unit, new_width)
        old += width
        new += new_width
        offsets[old] = new
    adjusted = copy.deepcopy(list(entities))
    for entity in adjusted:
        start, end = entity['offset'], entity['offset'] + entity['length']
        entity['offset'] = offsets[start]
        entity['length'] = offsets[end] - offsets[start]
    return ''.join(result), adjusted


def styled_text(text, html=False):
    if not html:
        return _plain(text)[0]
    result, literal = [], 0
    for part in _HTML.split(text):
        if part.startswith('<'):
            if re.match(r'<(?:code|pre)(?:\s|>)', part, re.I):
                literal += 1
            elif re.match(r'</(?:code|pre)\s*>', part, re.I):
                literal = max(0, literal - 1)
            result.append(part)
        elif part.startswith('&') or literal:
            result.append(part)
        else:
            result.append(_plain(part)[0])
    return ''.join(result)


def format_bot_payload(method, payload):
    result = copy.deepcopy(payload)
    if method == 'sendInvoice':
        for key in ('title', 'description'):
            if isinstance(result.get(key), str):
                result[key] = styled_text(result[key])
    if method == 'setChatMenuButton' and isinstance(result.get('menu_button', {}).get('text'), str):
        result['menu_button']['text'] = styled_text(result['menu_button']['text'])
    if method == 'setMyCommands':
        for command in result.get('commands', []):
            if isinstance(command.get('description'), str):
                command['description'] = styled_text(command['description'])
    if method in {'sendMessage', 'editMessageText', 'answerCallbackQuery'} and isinstance(result.get('text'), str):
        mode = str(result.get('parse_mode', '')).lower()
        if mode == 'html':
            result['text'] = styled_text(result['text'], html=True)
        elif not mode:
            result['text'], entities = _plain(result['text'], result.get('entities', ()))
            if 'entities' in result:
                result['entities'] = entities
    markup = result.get('reply_markup', {})
    if isinstance(markup, dict):
        for key in ('inline_keyboard', 'keyboard'):
            for row in markup.get(key, []):
                for button in row:
                    if isinstance(button, dict) and isinstance(button.get('text'), str):
                        button['text'] = styled_text(button['text'])
    return result
