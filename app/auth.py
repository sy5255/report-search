from ldap3 import Server, Connection
import jwt
import datetime
from app.config import JWT_KEY

def check_auth(id: str, pw: str):
    server = Server('11.11.111.111', port=123) 
    try:
        conn = Connection(
            server,
            'CN=' + id.strip() + ',OU=Employee,OU=Users,OU=KOR,OU=Locations,DC=ss,DC=net', 
            pw.strip(),
            auto_bind=True
        )
    except:
        return ""

    token = jwt.encode(
        payload={
            "id": id,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=6)  # 세션 좀 길게(원하면 1시간으로)
        },
        key=JWT_KEY,
        algorithm='HS256'
    )
    return token

def validate_jwt(token):
    if token:
        try:
            decode_token = jwt.decode(token, JWT_KEY, algorithms='HS256')
        except:
            return ""
        return decode_token.get("id", "")
    return ""