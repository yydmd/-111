import os
from .encrypt import AES_Encrypt, generate_captcha_key, enc, verify_param
from .reserve import reserve

def _fetch_env_variables(env_name, action):
    if not action:
        return ""
    try:
        return os.environ[env_name]
    except KeyError:
        # CI mode without credentials must fail fast with a clear cause
        # instead of returning None for callers to crash on later.
        raise SystemExit(f"环境变量 {env_name} 未配置：--action/CI 模式必须提供账号凭据") from None

def get_user_credentials(action):
    usernames = _fetch_env_variables('USERNAMES', action)
    passwords = _fetch_env_variables('PASSWORDS', action)
    return usernames, passwords