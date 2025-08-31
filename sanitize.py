import re

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PHONE_RE = re.compile(r'\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}\b')
ADDRESS_HINT_RE = re.compile(r'(丁目|番地|号|区|市|町|村|都|道|府|県)')
NAME_HINT_RE = re.compile(r'(さん|氏|様|くん|ちゃん)')

def _mask(label: str) -> str:
    return f'［{label}］'

def _mask_name_hints(s: str) -> str:
    out, i = [], 0
    for m in NAME_HINT_RE.finditer(s):
        start = max(0, m.start()-4)
        out.append(s[i:start] + _mask('氏名') + s[m.start():m.end()])
        i = m.end()
    out.append(s[i:])
    return ''.join(out)

def sanitize_public(text: str) -> str:
    if not text:
        return text
    t = text
    t = EMAIL_RE.sub(_mask('メール'), t)
    t = URL_RE.sub(_mask('URL'), t)
    t = HANDLE_RE.sub(_mask('ハンドル'), t)
    t = POSTAL_RE.sub(_mask('郵便'), t)
    t = PHONE_RE.sub(_mask('電話'), t)
    t = _mask_name_hints(t)
    return t