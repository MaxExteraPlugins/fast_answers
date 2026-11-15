# -*- coding: utf-8 -*-
from typing import Any, List
import json
import re
import traceback

from java.util import ArrayList, Locale
from java.lang import Boolean, Class, Object

from org.telegram.tgnet import TLRPC
from org.telegram.messenger import AndroidUtilities
from org.telegram.ui.ActionBar import Theme
from org.telegram.ui.Components import TextStyleSpan, QuoteSpan

from base_plugin import BasePlugin, MethodHook
from android_utils import run_on_ui_thread, OnClickListener
from client_utils import get_last_fragment
from hook_utils import find_class, get_private_field, set_private_field
from ui.settings import Divider, Header, Selector, Switch, Text
from ui.alert import AlertDialogBuilder
from ui.bulletin import BulletinHelper

__id__ = "fast_answers"
__name__ = "Fast Answers"
__description__ = ("Fast answers for chats: start typing a message and a panel with ready "
                   "answers appears above the keyboard.\n\n"
                   "Быстрые ответы в чате: начните писать сообщение — над клавиатурой "
                   "появится панель с готовыми ответами.")
__author__ = "@itNotMax - @MaxExteraPlugins"
__version__ = "1.0.1"
__icon__ = "MaxExteraPluginsStickers/28"
__min_version__ = "12.10.6"
__app_version__ = ">=12.1.1"
__sdk_version__ = ">=1.4.3.3"

ENTRIES_KEY = "entries"
TEXT_SEARCH_KEY = "search_by_text"
MIN_LEN_KEY = "min_query_len"
SEND_KEY = "send_on_tap"
ID_PREFIX = "fa_answers:"
MAX_QUERY_LEN = 64

STYLE_BOLD = 1
STYLE_ITALIC = 2
STYLE_MONO = 4
STYLE_STRIKE = 8
STYLE_UNDERLINE = 16

SPAN_EXCLUSIVE = 33

_wrapped_containers = set()

try:
    from extera_utils.classes import Base, java_subclass, joverride, jfield
except Exception:
    pass


def _activity():
    try:
        f = get_last_fragment()
        if f is not None:
            return f.getParentActivity()
    except Exception:
        pass
    return None


class _WrappedClickListener(object):

    def __init__(self, plugin, container, original):
        self._plugin = plugin
        self._container = container
        self._original = original

    def onItemClick(self, view, position):
        plugin = self._plugin
        try:
            if not plugin.active:
                self._call_original(view, position)
                return
            container = self._container
            adapter = container.getAdapter()
            if adapter is None:
                self._call_original(view, position)
                return
            item = adapter.getItem(position)
            if item is None:
                self._call_original(view, position)
                return
            from org.telegram.tgnet import TLRPC
            if isinstance(item, TLRPC.TL_botInlineResult):
                res_id = str(item.id) if item.id is not None else ""
                if res_id.startswith(ID_PREFIX):
                    payload = plugin.pop_answer(res_id)
                    if payload:
                        answer = payload.get("a") or ""
                        if answer:
                            fragment = get_last_fragment()
                            if fragment is not None:
                                ev = get_private_field(fragment, "chatActivityEnterView")
                                if ev is not None:
                                    ev.setFieldText(answer)
                                    plugin.apply_styles_to_input(ev, payload)
                                    plugin.log("fast_answers: wrapped click: intercepted, answer_len=" + str(len(answer)))
                                    try:
                                        from android.view import View
                                        container.setVisibility(View.GONE)
                                    except Exception:
                                        pass
                                    return
            self._call_original(view, position)
        except Exception as e:
            plugin.log("fast_answers: wrapped click error: " + repr(e))
            self._call_original(view, position)

    def _call_original(self, view, position):
        if self._original is not None:
            try:
                self._original.onItemClick(view, position)
            except Exception:
                pass


class _SearchHook(MethodHook):

    def __init__(self, plugin):
        MethodHook.__init__(self)
        self._plugin = plugin

    def before_hooked_method(self, param):
        plugin = self._plugin
        try:
            if not plugin.active:
                return
            if not param.args or param.args[0] is None:
                return
            text = str(param.args[0])
            if not text.strip():
                return
            answers = plugin.find_answers(text)
            if not answers:
                return
            plugin.inject_answers(answers, param.thisObject)
            param.setResult(None)
        except Exception as e:
            plugin.log("fast_answers: search hook error: " + repr(e))


class _SendHook(MethodHook):

    def __init__(self, plugin):
        MethodHook.__init__(self)
        self._plugin = plugin

    def before_hooked_method(self, param):
        plugin = self._plugin
        try:
            plugin.log("fast_answers: _SendHook entered, args=" + repr(param.args))
            if not param.args or param.args[0] is None:
                plugin.log("fast_answers: _SendHook: no args or null result")
                return
            result = param.args[0]
            res_id = str(result.id) if result.id is not None else ""
            plugin.log("fast_answers: _SendHook: res_id=" + res_id)
            if not res_id.startswith(ID_PREFIX):
                return
            payload = plugin.pop_answer(res_id)
            if not payload:
                plugin.log("fast_answers: _SendHook: no payload for " + res_id)
                return
            answer = payload.get("a") or ""
            if not answer:
                return
            param.setResult(None)
            plugin.log("fast_answers: _SendHook: setting text, answer_len=" + str(len(answer)))
            chat_activity = param.thisObject
            if chat_activity is None:
                return
            try:
                ev = get_private_field(chat_activity, "chatActivityEnterView")
                plugin.log("fast_answers: _SendHook: chatActivityEnterView is None=" + str(ev is None))
                if ev is not None:
                    ev.setFieldText(answer)
                    plugin.apply_styles_to_input(ev, payload)
                    if plugin.get_setting(SEND_KEY, False):
                        plugin.send_now(chat_activity, ev)
            except Exception as ex:
                plugin.log("fast_answers: _SendHook enterView error: " + repr(ex))
            try:
                from android.view import View
                container = get_private_field(chat_activity, "mentionContainer")
                if container is not None:
                    container.setVisibility(View.GONE)
            except Exception:
                pass
        except Exception as e:
            plugin.log("fast_answers: send hook error: " + repr(e))


class _UpdateVisibilityHook(MethodHook):

    def __init__(self, plugin):
        MethodHook.__init__(self)
        self._plugin = plugin

    def before_hooked_method(self, param):
        plugin = self._plugin
        try:
            if not plugin.active:
                return
            bl = param.args[0] if param.args else False
            if not bl:
                return
            container = param.thisObject
            if container is None:
                return
            cid = id(container)
            if cid in _wrapped_containers:
                return
            list_view = get_private_field(container, "listView")
            if list_view is None:
                return
            current_listener = get_private_field(container, "mentionsOnItemClickListener")
            if current_listener is None:
                return
            wrapped = _WrappedClickListener(plugin, container, current_listener)
            list_view.setOnItemClickListener(wrapped)
            _wrapped_containers.add(cid)
            plugin.log("fast_answers: updateVisibility hook: wrapped click listener, container_id=" + str(cid))
        except Exception as e:
            plugin.log("fast_answers: updateVisibility hook error: " + repr(e))



# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================

# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================

# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================
class AuthorChannelHelper:
    CONFIG_URL = "https://raw.githubusercontent.com/MaxExteraPlugins/config/main/links.json"

    DEFAULT_LINKS = {
        "channel_username": "MaxExteraPlugins",
        "channel_url": "https://t.me/MaxExteraPlugins",
        "trial_days": 7,
        "header": {"ru": "MaxExteraPlugins", "en": "MaxExteraPlugins"},
        "buttons": [
            {"title": {"ru": "Канал автора", "en": "Author's Channel"}, "icon": "msg_channel", "url": "https://t.me/MaxExteraPlugins"},
            {"title": {"ru": "Поддержать автора", "en": "Support author"}, "icon": "msg_share", "url": "https://t.me/itNotMax"},
            {"title": {"ru": "Сообщить о баге", "en": "Report a bug"}, "icon": "msg_help", "url": "https://t.me/itNotMax"}
        ]
    }

    _TEXTS = {
        "install_card_title": {"ru": "Спасибо за установку!", "en": "Thank you for installing!"},
        "install_card_msg": {
            "ru": "Надеемся, что плагин будет вам полезен!\n\nМы делаем плагины бесплатными и доступными для всех: пожалуйста, поддержите нас подпиской. Там будут только важные новости, обновления и розыгрыши.",
            "en": "We hope this plugin will be useful to you!\n\nWe keep our plugins free and open for everyone: please support us with a subscription. We only post key news, updates, and giveaways."
        },
        "subscribe_btn": {"ru": "Подписаться", "en": "Subscribe"},
        "remind_later_btn": {"ru": "Напомнить позже", "en": "Remind later"},
        "check_sub_btn": {"ru": "Проверить подписку", "en": "Check subscription"},
        "trial_expired_title": {"ru": "Пожалуйста, подпишитесь на канал", "en": "Please subscribe to the channel"},
        "trial_expired_msg": {
            "ru": "Пожалуйста, подпишитесь на наш официальный канал.\n\nМы делаем плагины бесплатно, ничего не требуя взамен. До подписки функции плагина временно приостановлены.",
            "en": "Please subscribe to our official channel.\n\nWe develop plugins for free and ask nothing in return. Plugin features are paused until you subscribe."
        },
        "sub_thank_you": {"ru": "Спасибо за подписку на наш канал!", "en": "Thank you for subscribing to our channel!"},
        "sub_thank_you_after_expired": {"ru": "Спасибо за подписку на наш канал!\nПлагин продолжит работу!", "en": "Thank you for subscribing to our channel!\nThe plugin will continue working!"},
        "sub_not_found": {"ru": "Подписка не обнаружена. Пожалуйста, подпишитесь на канал.", "en": "Subscription not found. Please subscribe to the channel."},
        "settings_channel": {"ru": "Канал автора", "en": "Author's Channel"},
        "settings_donate": {"ru": "Поддержать автора", "en": "Support author"},
        "settings_bug_report": {"ru": "Сообщить о баге", "en": "Report a bug"}
    }

    @classmethod
    def is_ru(cls) -> bool:
        try:
            from org.telegram.messenger import LocaleController
            cur = str(LocaleController.getInstance().getCurrentLocale().getLanguage() or "").lower()
            if cur.startswith(("ru", "uk", "be")): return True
            if cur: return False
        except Exception:
            pass
        try:
            from java.util import Locale
            code = str(Locale.getDefault().getLanguage() or "").lower()
            if code.startswith(("ru", "uk", "be")): return True
        except Exception:
            pass
        return False

    @classmethod
    def tr(cls, key: str) -> str:
        entry = cls._TEXTS.get(key, {})
        return entry.get("ru" if cls.is_ru() else "en", entry.get("en", key))

    def __init__(self, plugin, icon_sticker=None):
        self.plugin = plugin
        self.icon_sticker = icon_sticker or getattr(plugin, "__icon__", "MaxExteraPluginsStickers/1")
        self._stopped = False
        self._active_dialog = None
        self._last_known_sub = None

    def on_load(self):
        self._stopped = False
        self._active_dialog = None
        self._last_known_sub = None
        self._load_remote_links_async()
        self._check_initial_subscription_flow()
        self._start_subscription_watcher()

    def on_event(self, event_type):
        try:
            name = str(event_type).upper()
            if name.endswith("START") or name.endswith("RESUME"):
                self._check_subscription_change_instant()
        except Exception:
            pass

    def on_unload(self):
        self._stopped = True
        self._dismiss_active_dialog()

    def _get_links(self) -> dict:
        raw = self.plugin.get_setting("remote_links_cache", None)
        if raw:
            try:
                import json
                data = json.loads(raw)
                if isinstance(data, dict): return data
            except Exception:
                pass
        return self.DEFAULT_LINKS

    def _load_remote_links_async(self):
        def worker():
            try:
                import urllib.request, json
                req = urllib.request.Request(self.CONFIG_URL, headers={"User-Agent": "MaxExteraPlugins"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if isinstance(data, dict):
                            self.plugin.set_setting("remote_links_cache", json.dumps(data))
                            self._resolve_channel_in_telegram(data.get("channel_username", "MaxExteraPlugins"))
            except Exception:
                pass
        import threading
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _open_url(self, url: str, view=None):
        try:
            from org.telegram.messenger.browser import Browser
            from org.telegram.messenger import ApplicationLoader
            from client_utils import get_last_fragment
            ctx = None
            if view is not None:
                ctx = view.getContext()
            if ctx is None:
                frag = get_last_fragment()
                if frag is not None:
                    ctx = frag.getParentActivity()
            if ctx is None:
                ctx = ApplicationLoader.applicationContext
            Browser.openUrl(ctx, url)
        except Exception:
            pass

    def _resolve_channel_in_telegram(self, username: str):
        try:
            from org.telegram.messenger import UserConfig, MessagesController
            from org.telegram.tgnet import TLRPC, ConnectionsManager, RequestDelegate
            clean_user = username.lstrip("@")
            acc = int(UserConfig.selectedAccount)
            mc = MessagesController.getInstance(acc)
            if not mc: return
            req = TLRPC.TL_contacts_resolveUsername()
            req.username = clean_user
            helper_ref = self
            class Delegate(RequestDelegate):
                def run(self, response, error):
                    if error is None and response is not None:
                        chats = getattr(response, "chats", None)
                        if chats is not None and chats.size() > 0:
                            mc.putChats(chats, False)
                            c = chats.get(0)
                            cid = getattr(c, "id", 0)
                            if cid > 0:
                                helper_ref.plugin.set_setting("resolved_channel_id", int(cid))
            ConnectionsManager.getInstance(acc).sendRequest(req, Delegate())
        except Exception:
            pass

    def is_subscribed(self) -> bool:
        try:
            from org.telegram.messenger import UserConfig, MessagesController
            links = self._get_links()
            target = str(links.get("channel_username", "MaxExteraPlugins") or "MaxExteraPlugins").lower().lstrip("@")
            saved_cid = self.plugin.get_setting("resolved_channel_id", 0)

            for acc in range(UserConfig.MAX_ACCOUNT_COUNT):
                uconf = UserConfig.getInstance(acc)
                if not uconf.isClientActivated(): continue
                mc = MessagesController.getInstance(acc)
                if not mc: continue

                if saved_cid:
                    try:
                        from java.lang import Long as JLong
                        c = mc.getChat(JLong(int(saved_cid)))
                        if c is not None:
                            left = bool(getattr(c, "left", True))
                            kicked = bool(getattr(c, "kicked", False))
                            if not left and not kicked: return True
                    except Exception:
                        pass

                try:
                    chat = mc.getUserOrChat(target)
                    if chat is not None:
                        cid = getattr(chat, "id", 0)
                        if cid and not saved_cid:
                            self.plugin.set_setting("resolved_channel_id", int(cid))
                        left = bool(getattr(chat, "left", True))
                        kicked = bool(getattr(chat, "kicked", False))
                        if not left and not kicked: return True
                except Exception:
                    pass

                try:
                    dialogs = mc.allDialogs
                    if dialogs is not None:
                        limit = min(dialogs.size(), 300)
                        for i in range(limit):
                            d = dialogs.get(i)
                            did = getattr(d, "id", 0)
                            if did < 0:
                                from java.lang import Long as JLong
                                c = mc.getChat(JLong(-did))
                                if c is not None:
                                    uname = str(getattr(c, "username", "") or "").lower()
                                    if uname == target:
                                        cid = getattr(c, "id", 0)
                                        if cid and not saved_cid:
                                            self.plugin.set_setting("resolved_channel_id", int(cid))
                                        left = bool(getattr(c, "left", True))
                                        kicked = bool(getattr(c, "kicked", False))
                                        if not left and not kicked: return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def is_allowed(self) -> bool:
        import time
        if self.is_subscribed():
            if not self.plugin.get_setting("was_subscribed", False):
                was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                self.plugin.set_setting("was_subscribed", True)
                self.plugin.set_setting("was_expired_state", False)
                self._notify_subscribed(was_expired=was_exp)
            return True

        if self.plugin.get_setting("was_expired_state", False) or self.plugin.get_setting("was_subscribed", False):
            self.plugin.set_setting("was_subscribed", False)
            self.plugin.set_setting("was_expired_state", True)
            self._show_expired_card()
            return False

        now = time.time()
        install_time = float(self.plugin.get_setting("install_time", 0.0) or 0.0)
        if install_time <= 0:
            install_time = now
            self.plugin.set_setting("install_time", install_time)

        trial_days = float(self._get_links().get("trial_days", 7))
        trial_sec = trial_days * 86400.0

        if (now - install_time) < trial_sec:
            return True

        self.plugin.set_setting("was_expired_state", True)
        self._show_expired_card()
        return False

    def prepend_settings(self, original_items=None) -> list:
        from ui.settings import Header, Text, Divider

        # Prevent double-prepending
        if original_items and any(getattr(x, "text", "") == "MaxExteraPlugins" for x in original_items if type(x).__name__ == "Header"):
            return original_items

        links = self._get_links()
        buttons_config = links.get("buttons")

        author_items = []
        if isinstance(buttons_config, list):
            for btn in buttons_config:
                if not isinstance(btn, dict): continue
                raw_url = btn.get("url")
                if isinstance(raw_url, dict):
                    keys = [
                        getattr(self.plugin, "id", None),
                        globals().get("__id__", None),
                        globals().get("__name__", None),
                        self.plugin.__class__.__name__
                    ]
                    keys += [k.lower() for k in keys if isinstance(k, str)]
                    resolved_url = ""
                    for k in keys:
                        if k and k in raw_url:
                            resolved_url = str(raw_url[k] or "").strip()
                            if resolved_url:
                                break
                    url = resolved_url
                elif isinstance(raw_url, str):
                    url = raw_url.strip()
                else:
                    url = ""

                if not url: continue

                title_data = btn.get("title")
                if isinstance(title_data, dict):
                    title = title_data.get("ru" if self.is_ru() else "en") or title_data.get("en") or title_data.get("ru") or ""
                elif isinstance(title_data, str) and title_data:
                    title = title_data
                else:
                    title = btn.get("title_ru" if self.is_ru() else "title_en") or btn.get("title_ru") or btn.get("title_en") or ""

                if not title: continue
                icon = str(btn.get("icon") or "msg_channel").strip()

                def make_click(u):
                    return lambda v=None: self._open_url(u, v)

                author_items.append(Text(text=title, icon=icon, on_click=make_click(url)))
        else:
            for k, text_key, icon_name in [
                ("channel_url", "settings_channel", "msg_channel"),
                ("donate_url", "settings_donate", "msg_share"),
                ("bug_report_url", "settings_bug_report", "msg_help")
            ]:
                u = str(links.get(k) or "").strip()
                if u:
                    author_items.append(Text(text=self.tr(text_key), icon=icon_name, on_click=lambda v=None, target_u=u: self._open_url(target_u, v)))

        res = []
        if author_items:
            hdr = links.get("header") or "MaxExteraPlugins"
            if isinstance(hdr, dict):
                hdr_text = hdr.get("ru" if self.is_ru() else "en") or hdr.get("ru") or hdr.get("en") or "MaxExteraPlugins"
            else:
                hdr_text = str(hdr)
            res.append(Header(text=hdr_text))
            res.extend(author_items)
            res.append(Divider())

        # Clean original_items by removing any old "Информация" / "Information" category
        cleaned_items = []
        if original_items:
            skipping_info = False
            for item in original_items:
                item_cls = type(item).__name__
                if item_cls == "Header":
                    txt = str(getattr(item, "text", "") or getattr(item, "title", "") or "").lower()
                    if "информ" in txt or "inform" in txt:
                        skipping_info = True
                        continue
                    else:
                        skipping_info = False
                elif skipping_info and item_cls == "Divider":
                    skipping_info = False
                    continue

                if not skipping_info:
                    cleaned_items.append(item)

        res.extend(cleaned_items)
        return res

    def _dismiss_active_dialog(self):
        try:
            if self._active_dialog is not None:
                self._active_dialog.dismiss()
        except Exception:
            pass
        self._active_dialog = None

    def _is_dialog_active(self) -> bool:
        try:
            if self._active_dialog is not None:
                return bool(self._active_dialog.isShowing())
        except Exception:
            pass
        return False

    def _notify_subscribed(self, was_expired=False):
        self.plugin.set_setting("was_subscribed", True)
        from ui.bulletin import BulletinHelper
        msg_key = "sub_thank_you_after_expired" if was_expired else "sub_thank_you"
        BulletinHelper.show_success(self.tr(msg_key))

    def _check_subscription_change_instant(self):
        try:
            from android_utils import run_on_ui_thread
            cur_sub = self.is_subscribed()
            prev_sub = self._last_known_sub
            if prev_sub is not None and prev_sub != cur_sub:
                self._last_known_sub = cur_sub
                if cur_sub:
                    was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                    self.plugin.set_setting("was_subscribed", True)
                    self.plugin.set_setting("was_expired_state", False)
                    self._dismiss_active_dialog()
                    self._notify_subscribed(was_expired=was_exp)
                else:
                    self.plugin.set_setting("was_subscribed", False)
                    self.plugin.set_setting("was_expired_state", True)
                    self._dismiss_active_dialog()
                    self._show_expired_card()
            else:
                self._last_known_sub = cur_sub
        except Exception:
            pass

    def _start_subscription_watcher(self):
        def watcher_loop():
            import time
            from android_utils import run_on_ui_thread
            time.sleep(2.0)
            if self._stopped: return
            self._last_known_sub = self.is_subscribed()

            while not self._stopped:
                try:
                    time.sleep(2.0)
                    if self._stopped: break
                    cur_sub = self.is_subscribed()
                    prev_sub = self._last_known_sub

                    if prev_sub is not None and prev_sub != cur_sub:
                        self._last_known_sub = cur_sub
                        if cur_sub:
                            was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                            self.plugin.set_setting("was_subscribed", True)
                            self.plugin.set_setting("was_expired_state", False)
                            def on_sub():
                                self._dismiss_active_dialog()
                                self._notify_subscribed(was_expired=was_exp)
                            run_on_ui_thread(on_sub)
                        else:
                            self.plugin.set_setting("was_subscribed", False)
                            self.plugin.set_setting("was_expired_state", True)
                            def on_unsub():
                                self._dismiss_active_dialog()
                                self._show_expired_card()
                            run_on_ui_thread(on_unsub)
                    else:
                        self._last_known_sub = cur_sub
                except Exception:
                    pass
        import threading
        t = threading.Thread(target=watcher_loop, daemon=True)
        t.start()

    def _check_initial_subscription_flow(self):
        def flow():
            import time
            try:
                now = time.time()
                install_time = float(self.plugin.get_setting("install_time", 0.0) or 0.0)
                if install_time <= 0:
                    install_time = now
                    self.plugin.set_setting("install_time", install_time)

                is_sub = self.is_subscribed()
                self._last_known_sub = is_sub
                was_sub = bool(self.plugin.get_setting("was_subscribed", False))
                was_exp = bool(self.plugin.get_setting("was_expired_state", False))

                if is_sub:
                    if not was_sub:
                        self.plugin.set_setting("was_subscribed", True)
                        self.plugin.set_setting("was_expired_state", False)
                        self._notify_subscribed(was_expired=was_exp)
                    return

                if was_sub or was_exp:
                    self.plugin.set_setting("was_subscribed", False)
                    self.plugin.set_setting("was_expired_state", True)
                    self._show_expired_card()
                    return

                trial_days = float(self._get_links().get("trial_days", 7))
                trial_sec = trial_days * 86400.0
                last_remind = float(self.plugin.get_setting("last_remind_time", 0.0) or 0.0)

                if (now - install_time) < trial_sec:
                    if last_remind <= 0 or (now - last_remind) >= trial_sec:
                        self._show_welcome_card()
                else:
                    self.plugin.set_setting("was_expired_state", True)
                    self._show_expired_card()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(flow, 1500)

    def _create_card_content(self, act, title_text, message_text):
        try:
            from android.widget import LinearLayout, TextView
            from android.view import Gravity
            from android.graphics import Typeface
            from org.telegram.messenger import AndroidUtilities, UserConfig, MediaDataController
            from org.telegram.ui.ActionBar import Theme
            from org.telegram.ui.Components import BackupImageView

            dp = AndroidUtilities.dp
            layout = LinearLayout(act)
            layout.setOrientation(LinearLayout.VERTICAL)
            layout.setGravity(Gravity.CENTER_HORIZONTAL)
            layout.setPadding(dp(24), dp(16), dp(24), dp(6))

            size = dp(76)
            iv = BackupImageView(act)
            iv.setRoundRadius(dp(18))
            lp_iv = LinearLayout.LayoutParams(size, size)
            lp_iv.gravity = Gravity.CENTER_HORIZONTAL
            lp_iv.setMargins(0, dp(4), 0, dp(16))
            iv.setLayoutParams(lp_iv)

            try:
                acc = int(UserConfig.selectedAccount)
                mdc = MediaDataController.getInstance(acc)
                raw_icon = str(self.icon_sticker)
                if "/" in raw_icon:
                    pack, idx_s = raw_icon.split("/", 1)
                    mdc.setPlaceholderImageByIndex(iv, pack, int(idx_s), "150_150")
            except Exception:
                pass
            layout.addView(iv)

            if title_text:
                tt = TextView(act)
                tt.setText(title_text)
                tt.setTextSize(19.0)
                tt.setTypeface(None, Typeface.BOLD)
                tt.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                tt.setGravity(Gravity.CENTER_HORIZONTAL)
                lp_t = LinearLayout.LayoutParams(-1, -2)
                lp_t.setMargins(0, 0, 0, dp(12))
                tt.setLayoutParams(lp_t)
                layout.addView(tt)

            if message_text:
                mt = TextView(act)
                mt.setText(message_text)
                mt.setTextSize(15.0)
                mt.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                mt.setLineSpacing(float(dp(3)), 1.0)
                mt.setGravity(Gravity.CENTER_HORIZONTAL)
                lp_m = LinearLayout.LayoutParams(-1, -2)
                mt.setLayoutParams(lp_m)
                layout.addView(mt)

            return layout
        except Exception:
            return None

    def _show_welcome_card(self):
        def show():
            try:
                if self._is_dialog_active(): return
                from client_utils import get_last_fragment
                frag = get_last_fragment()
                if not frag: return
                act = frag.getParentActivity()
                if not act: return

                links = self._get_links()
                url = links.get("channel_url", "https://t.me/MaxExteraPlugins")

                from ui.alert import AlertDialogBuilder
                b = AlertDialogBuilder(act)
                content = self._create_card_content(act, self.tr("install_card_title"), self.tr("install_card_msg"))
                if content is not None:
                    b.set_view(content)
                else:
                    b.set_title(self.tr("install_card_title"))
                    b.set_message(self.tr("install_card_msg"))

                def on_sub(d, w):
                    self._dismiss_active_dialog()
                    self._open_url(url)
                    def check_after():
                        if self.is_subscribed():
                            self._notify_subscribed(was_expired=False)
                    from android_utils import run_on_ui_thread
                    run_on_ui_thread(check_after, 3000)

                def on_later(d, w):
                    import time
                    self._dismiss_active_dialog()
                    self.plugin.set_setting("last_remind_time", time.time())

                b.set_positive_button(self.tr("subscribe_btn"), on_sub)
                b.set_negative_button(self.tr("remind_later_btn"), on_later)
                self._active_dialog = b.show()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(show)

    def _show_expired_card(self):
        def show():
            try:
                if self._is_dialog_active(): return
                from client_utils import get_last_fragment
                frag = get_last_fragment()
                if not frag: return
                act = frag.getParentActivity()
                if not act: return

                links = self._get_links()
                url = links.get("channel_url", "https://t.me/MaxExteraPlugins")

                from ui.alert import AlertDialogBuilder
                from ui.bulletin import BulletinHelper
                b = AlertDialogBuilder(act)
                content = self._create_card_content(act, self.tr("trial_expired_title"), self.tr("trial_expired_msg"))
                if content is not None:
                    b.set_view(content)
                else:
                    b.set_title(self.tr("trial_expired_title"))
                    b.set_message(self.tr("trial_expired_msg"))

                def on_sub(d, w):
                    self._dismiss_active_dialog()
                    self._open_url(url)
                    def check_after():
                        if self.is_subscribed():
                            self.plugin.set_setting("was_subscribed", True)
                            self.plugin.set_setting("was_expired_state", False)
                            self._notify_subscribed(was_expired=True)
                    from android_utils import run_on_ui_thread
                    run_on_ui_thread(check_after, 3000)

                def on_check(d, w):
                    self._dismiss_active_dialog()
                    if self.is_subscribed():
                        self.plugin.set_setting("was_subscribed", True)
                        self.plugin.set_setting("was_expired_state", False)
                        self._notify_subscribed(was_expired=True)
                    else:
                        BulletinHelper.show_error(self.tr("sub_not_found"))
                        from android_utils import run_on_ui_thread
                        run_on_ui_thread(self._show_expired_card, 500)

                b.set_positive_button(self.tr("subscribe_btn"), on_sub)
                b.set_negative_button(self.tr("check_sub_btn"), on_check)
                self._active_dialog = b.show()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(show)


class FastAnswers(BasePlugin):

    def on_app_event(self, event_type):
        if hasattr(self, "_author_helper"):
            self._author_helper.on_event(event_type)

    def on_plugin_load(self):
        self._author_helper = AuthorChannelHelper(self, icon_sticker=globals().get("__icon__", "MaxExteraPluginsStickers/1"))
        self._author_helper.on_load()
        self.active = True
        self._hook_refs = []
        self._answer_by_id = {}
        self._seq = 0
        self._lang = self._detect_lang()
        self._entries = self._get_entries()
        self._install_hooks()

    def _detect_lang(self):
        lang = "en"
        try:
            code = str(Locale.getDefault().getLanguage()).lower()
            if code == "ru":
                lang = "ru"
        except Exception:
            pass
        return lang

    def _tr(self, ru_text, en_text):
        if getattr(self, "_lang", "ru") == "en":
            return en_text
        return ru_text

    def _min_len(self):
        try:
            v = int(self.get_setting(MIN_LEN_KEY, 2))
        except Exception:
            v = 2
        return max(0, min(5, v))

    def _default_entries(self):
        return []

    def on_plugin_unload(self):
        if hasattr(self, "_author_helper"):
            self._author_helper.on_unload()
        self.active = False
        for ref in list(getattr(self, "_hook_refs", [])):
            try:
                self.unhook_method(ref)
            except Exception:
                pass
        self._hook_refs = []
        self._answer_by_id = {}

    # ------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------

    def _install_hooks(self):
        targets = (
            ("org.telegram.ui.Adapters.MentionsAdapter", "searchUsernameOrHashtag", _SearchHook(self)),
            ("org.telegram.ui.ChatActivity", "sendBotInlineResult", _SendHook(self)),
            ("org.telegram.ui.Components.MentionsContainerView", "updateVisibility", _UpdateVisibilityHook(self)),
        )
        for class_name, method_name, hook in targets:
            try:
                cls = find_class(class_name)
                if cls is None:
                    self.log("fast_answers: class not found: " + class_name)
                    continue
                for m in list(cls.getClass().getDeclaredMethods()):
                    if str(m.getName()) == method_name:
                        try:
                            m.setAccessible(True)
                        except Exception:
                            pass
                        ref = self.hook_method(m, hook)
                        if ref is not None:
                            self._hook_refs.append(ref)
                        break
            except Exception as e:
                self.log("fast_answers: hook error %s.%s: %s" % (class_name, method_name, repr(e)))

    # ------------------------------------------------------------
    # settings
    # ------------------------------------------------------------

    def _get_entries(self):
        raw = self.get_setting(ENTRIES_KEY, None)
        if raw is None:
            return self._default_entries()
        try:
            entries = json.loads(raw)
            if not isinstance(entries, list):
                return self._default_entries()
            out = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                answer = str(item.get("a") or "").strip()
                kws = []
                for kw in (item.get("k") or []):
                    kw = str(kw).strip().lower()
                    if kw:
                        kws.append(kw)
                if answer:
                    entry = {"a": answer}
                    if kws:
                        entry["k"] = kws
                    if isinstance(item.get("f"), list):
                        entry["f"] = item["f"]
                    if isinstance(item.get("q"), list):
                        entry["q"] = item["q"]
                    out.append(entry)
            return out
        except Exception:
            return list(DEFAULT_ENTRIES)

    def _save_entries(self, reload=True):
        self.set_setting(ENTRIES_KEY, json.dumps(self._entries, ensure_ascii=False), reload_settings=reload)

    def create_settings(self):
        try:
            items = self._inner_create_settings()
        except Exception:
            items = []
        if hasattr(self, '_author_helper'):
            return self._author_helper.prepend_settings(items)
        return items

    def _inner_create_settings(self):
        me = self
        if not hasattr(self, "_entries"):
            self._entries = self._get_entries()

        def open_guarded(index):
            try:
                me._open_entry_dialog(index)
            except Exception as e:
                me.log("fast_answers: dialog open error: " + repr(e))
                try:
                    me.log(traceback.format_exc())
                except Exception:
                    pass

        items = [
            Header(text=self._tr("Быстрые ответы", "Fast answers")),
            Switch(
                key=TEXT_SEARCH_KEY,
                text=self._tr("Поиск по тексту ответа", "Search answer text"),
                default=False,
                subtext=self._tr("Показывать ответы, если введённое сообщение встречается в тексте "
                                 "ответа, а не только в ключевых словах.",
                                 "Show answers when the typed message appears in the answer text, "
                                 "not only in keywords."),
            ),
            Selector(
                key=MIN_LEN_KEY,
                text=self._tr("Минимум символов для поиска", "Minimum characters to search"),
                default=2,
                items=["0", "1", "2", "3", "4", "5"],
            ),
            Switch(
                key=SEND_KEY,
                text=self._tr("Отправлять сразу при нажатии", "Send instantly on tap"),
                default=False,
                subtext=self._tr("Тап по ответу сразу отправит его в чат, а не вставит в поле ввода.",
                                 "Tapping an answer sends it right away instead of inserting it "
                                 "into the input field."),
            ),
            Divider(),
            Text(
                text=self._tr("Добавить быстрый ответ", "Add a quick answer"),
                icon="msg_arrow_forward",
                on_click=lambda v: run_on_ui_thread(lambda: open_guarded(None)),
            ),
        ]
        entries = self._entries
        if entries:
            items.append(Divider())
            items.append(Header(text=self._tr("Ответы (%d)", "Answers (%d)") % len(entries)))

            def make_click(idx):
                return lambda v: run_on_ui_thread(lambda: open_guarded(idx))

            def short_text(s, limit):
                s = str(s).replace("\n", " ").strip()
                if len(s) <= limit:
                    return s
                return s[:limit] + "…"

            for i, entry in enumerate(entries):
                kws = entry.get("k") or []
                sub = short_text(", ".join(kws), 60) if kws else " "
                items.append(Text(
                    text=short_text(entry["a"], 60),
                    subtext=sub,
                    on_click=make_click(i),
                ))
        else:
            items.append(Text(
                text=self._tr("Пока нет ответов. Добавьте первый!",
                              "No answers yet. Add the first one!"),
                icon="msg_info"))
        items.extend([
            Divider(),
            Text(
                text=self._tr("Как работает", "How it works"),
                icon="msg_help",
                subtext=self._tr("Подробная инструкция", "Detailed instructions"),
                on_click=lambda v: run_on_ui_thread(lambda: self._open_how_works()),
            ),
        ])
        if hasattr(self, "_author_helper"):

            return self._author_helper.prepend_settings(items)

        return items

    def _open_how_works(self):
        try:
            activity = _activity()
            if activity is None:
                BulletinHelper.show_error(self._tr("Не удалось получить активность.",
                                                   "Could not get the activity."))
                return
            from android.widget import ScrollView, TextView
            scr = ScrollView(activity)
            tv = TextView(activity)
            tv.setText(self._tr(
                "Начните писать сообщение в чате — как только введён минимум символов "
                "(указывается в настройках, по умолчанию: 2), над клавиатурой появится "
                "панель с быстрыми ответами, если по введённому тексту был найден быстрый "
                "ответ.\n\n"
                "По умолчанию ищется совпадение с ключевыми словами. Если включить "
                "«Поиск по тексту ответа», ответы будут находиться и по любому фрагменту "
                "их текста, а не только по ключевым словам.\n\n"
                "Ключевые слова можно не заполнять — такой ответ можно будет найти по "
                "тексту, если включён «Поиск по тексту ответа». Ключи без ограничений по "
                "длине, но короче установленного минимума никогда не сработают — плагин "
                "предупредит об этом при сохранении.\n\n"
                "Тап по ответу вставит его в поле ввода со всеми стилями: жирный, курсив, "
                "моноширинный, зачёркнутый, подчёркнутый, спойлер и цитата. Кнопка "
                "«Изменить в редакторе» открывает редактор, в котором можно оформить текст "
                "перед сохранением.",
                "Start typing a message in a chat — once the minimum number of characters "
                "is entered (set in the settings, 2 by default), a panel with quick answers "
                "appears above the keyboard if a quick answer was found for the typed text.\n\n"
                "By default, matches are searched against keywords. If «Search answer text» "
                "is enabled, answers are also found by any fragment of their text, not just "
                "keywords.\n\n"
                "Keywords are optional — such an answer can be found by text if «Search "
                "answer text» is enabled. Keywords have no length limit, but those shorter "
                "than the configured minimum will never match — the plugin warns about them "
                "on save.\n\n"
                "Tap an answer to insert it into the input field with all its styles: bold, "
                "italic, monospace, strikethrough, underline, spoiler and quote. The «Edit "
                "in editor» button opens an editor where you can style the text before saving."))
            tv.setTextSize(15)
            tv.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
            tv.setPadding(AndroidUtilities.dp(20), AndroidUtilities.dp(16),
                          AndroidUtilities.dp(20), AndroidUtilities.dp(16))
            tv.setTextIsSelectable(True)
            scr.addView(tv)
            bld = AlertDialogBuilder(activity)
            bld.set_title(self._tr("Как работает", "How it works"))
            bld.set_view(scr)
            bld.set_positive_button(self._tr("Понятно", "Got it"), lambda b, w: b.dismiss())
            bld.show()
        except Exception as e:
            self.log("fast_answers: how works error: " + repr(e))
            try:
                self.log(traceback.format_exc())
            except Exception:
                pass

    # ------------------------------------------------------------
    # entry dialog
    # ------------------------------------------------------------

    def _open_entry_dialog(self, index):
        me = self
        try:
            activity = _activity()
            if activity is None:
                me.log("fast_answers: no activity")
                BulletinHelper.show_error(self._tr("Не удалось получить активность.",
                                                   "Could not get the activity."))
                return
            entry = None
            if index is not None and 0 <= index < len(self._entries):
                entry = self._entries[index]

            from android.widget import EditText, LinearLayout, ScrollView, TextView
            from android.graphics.drawable import GradientDrawable

            scr = ScrollView(activity)
            lay = LinearLayout(activity)
            lay.setOrientation(LinearLayout.VERTICAL)
            pad = AndroidUtilities.dp(20)
            lay.setPadding(pad, pad // 2, pad, pad // 4)
            scr.addView(lay)

            def add_label(text):
                lbl = TextView(activity)
                lbl.setText(text)
                lbl.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                lbl.setTextSize(14)
                lbl.setTypeface(AndroidUtilities.bold())
                lbl.setPadding(0, AndroidUtilities.dp(10), 0, AndroidUtilities.dp(4))
                lay.addView(lbl)

            def add_input(hint, text, multiline):
                field = EditText(activity)
                field.setHint(hint)
                field.setSingleLine(not multiline)
                if multiline:
                    field.setMaxLines(6)
                if text:
                    field.setText(text)
                field.setTextSize(15)
                field.setTextColor(Theme.getColor(Theme.key_chat_messagePanelText))
                field.setHintTextColor(Theme.getColor(Theme.key_chat_messagePanelHint))
                bg = GradientDrawable()
                bg.setColor(Theme.getColor(Theme.key_chat_messagePanelBackground))
                bg.setCornerRadius(AndroidUtilities.dp(14))
                bg.setStroke(AndroidUtilities.dp(1), Theme.getColor(Theme.key_chat_messagePanelShadow))
                field.setBackground(bg)
                field.setPadding(
                    AndroidUtilities.dp(16), AndroidUtilities.dp(12),
                    AndroidUtilities.dp(16), AndroidUtilities.dp(12)
                )
                lay.addView(field)
                return field

            add_label(self._tr("ТЕКСТ ОТВЕТА", "ANSWER TEXT"))
            answer_field = add_input(self._tr("Например: 1.1 - оффтоп", "E.g.: 1.1 - off-topic"), entry["a"] if entry else "", True)

            format_row = LinearLayout(activity)
            format_row.setOrientation(LinearLayout.HORIZONTAL)
            lay.addView(format_row)
            fmt_btn = TextView(activity)
            fmt_btn.setText(self._tr("Изменить в редакторе", "Edit in editor"))
            fmt_btn.setTextSize(15)
            fmt_btn.setTextColor(self._accent_color())
            fmt_btn.setTypeface(AndroidUtilities.bold())
            fmt_btn.setPadding(AndroidUtilities.dp(0), AndroidUtilities.dp(8),
                               AndroidUtilities.dp(8), AndroidUtilities.dp(8))
            format_row.addView(fmt_btn)
            try:
                fmt_btn.setOnClickListener(OnClickListener(lambda v: me._open_format_editor(activity, answer_field)))
            except Exception as e:
                me.log("fast_answers: format button error: " + repr(e))

            if entry is not None and (entry.get("f") or entry.get("q")):
                try:
                    self._apply_runs(answer_field.getText(), entry.get("f"), entry.get("q"))
                except Exception as e:
                    me.log("fast_answers: preview runs error: " + repr(e))

            add_label(self._tr("КЛЮЧЕВЫЕ СЛОВА", "KEYWORDS"))
            kw_text = ", ".join(entry.get("k") or []) if entry else ""
            kw_field = add_input(self._tr("через запятую, можно оставить пустым",
                                          "comma-separated, can be left empty"),
                                 kw_text, False)

            bld = AlertDialogBuilder(activity)
            bld.set_title(self._tr("Добавить ответ", "Add answer") if entry is None
                          else self._tr("Изменить ответ", "Edit answer"))
            bld.set_view(scr)
            bld.set_negative_button(self._tr("Отмена", "Cancel"), lambda b, w: b.dismiss())
            bld.set_positive_button(self._tr("Сохранить", "Save"),
                                    lambda b, w: me._apply_entry(index, answer_field, kw_field, b))
            if entry is not None:
                bld.set_neutral_button(self._tr("Удалить", "Delete"),
                                       lambda b, w: me._delete_entry(index, b))
            bld.show()
        except Exception as e:
            me.log("fast_answers: dialog build error: " + repr(e))
            try:
                me.log(traceback.format_exc())
            except Exception:
                pass

    def _parse_keywords(self, kw_text):
        kws = []
        for part in re.split(r"[,\s]+", kw_text):
            part = part.strip().lower()
            if part:
                kws.append(part)
        return kws

    def _apply_entry(self, index, answer_field, kw_field, bld):
        try:
            editable = answer_field.getText()
            answer = str(editable).strip()
            runs, quotes = self._collect_runs(editable)
            kws = self._parse_keywords(str(kw_field.getText()))
        except Exception as e:
            self.log("fast_answers: save entry read error: " + repr(e))
            answer = ""
            runs = []
            quotes = []
            kws = []
        if not answer:
            bld.dismiss()
            BulletinHelper.show_error(self._tr("Заполните текст ответа.",
                                               "Fill in the answer text."))
            return
        entry = {"a": answer}
        if kws:
            entry["k"] = kws
        if runs:
            entry["f"] = runs
        if quotes:
            entry["q"] = quotes
        if index is not None and 0 <= index < len(self._entries):
            self._entries[index] = entry
        else:
            self._entries.append(entry)
        self._save_entries()
        bld.dismiss()
        limit = self._min_len()
        short_kws = [kw for kw in kws if len(kw) < limit]
        if short_kws and limit > 0:
            try:
                BulletinHelper.show_info(self._tr(
                    "Ключевые слова короче %d символов не смогут сработать: %s",
                    "Keywords shorter than %d characters will never match: %s")
                    % (limit, ", ".join(short_kws)))
            except Exception:
                pass

    def _delete_entry(self, index, bld):
        if index is not None and 0 <= index < len(self._entries):
            del self._entries[index]
            self._save_entries()
        bld.dismiss()

    # ------------------------------------------------------------
    # text formatting
    # ------------------------------------------------------------

    def _accent_color(self):
        try:
            return Theme.getColor(Theme.key_dialogTextBlue3)
        except Exception:
            try:
                return Theme.getColor(Theme.key_featuredStickers_addButton)
            except Exception:
                return 0x40FFFFFF

    def _fmt_state(self, field):
        flags = 0
        quote = False
        try:
            editable = field.getText()
            total = editable.length()
            s = field.getSelectionStart()
            e = field.getSelectionEnd()
            if s is None:
                s = 0
            if e is None:
                e = total
            if s > e:
                s, e = e, s
            s = max(0, min(s, total))
            e = max(0, min(e, total))
            if e <= s:
                s, e = 0, total
            for sp in editable.getSpans(s, e, TextStyleSpan):
                flags = flags | sp.getStyleFlags()
            quote = bool(editable.getSpans(s, e, QuoteSpan))
        except Exception:
            pass
        return flags, quote

    def _clear_format(self, field):
        try:
            editable = field.getText()
            for sp in list(editable.getSpans(0, editable.length(), TextStyleSpan)):
                editable.removeSpan(sp)
            for sp in list(editable.getSpans(0, editable.length(), QuoteSpan)):
                editable.removeSpan(sp)
        except Exception as e:
            self.log("fast_answers: clear format error: " + repr(e))

    def _clear_selection_format(self, field):
        try:
            editable = field.getText()
            total = editable.length()
            s = field.getSelectionStart()
            e = field.getSelectionEnd()
            if s is None:
                s = 0
            if e is None:
                e = total
            if s > e:
                s, e = e, s
            s = max(0, min(s, total))
            e = max(0, min(e, total))
            if e <= s:
                s, e = 0, total
            for sp in list(editable.getSpans(s, e, TextStyleSpan)):
                editable.removeSpan(sp)
            for sp in list(editable.getSpans(s, e, QuoteSpan)):
                editable.removeSpan(sp)
        except Exception as ex:
            self.log("fast_answers: clear selection format error: " + repr(ex))

    def _open_format_editor(self, activity, answer_field):
        me = self
        try:
            from android.app import Dialog
            from android.view import Gravity, WindowManager
            from android.graphics.drawable import ColorDrawable, GradientDrawable
            from android.widget import EditText, LinearLayout, TextView

            accent = self._accent_color()
            inactive = Theme.getColor(Theme.key_dialogTextBlack)
            hint = Theme.getColor(Theme.key_dialogTextHint)
            try:
                bg_color = Theme.getColor(Theme.key_dialogBackground)
            except Exception:
                bg_color = 0xFFFFFF

            src_editable = answer_field.getText()
            src_text = str(src_editable)
            src_runs, src_quotes = self._collect_runs(src_editable)

            root = LinearLayout(activity)
            root.setOrientation(LinearLayout.VERTICAL)
            root.setBackgroundColor(bg_color)
            root.setPadding(AndroidUtilities.dp(20), AndroidUtilities.dp(12),
                            AndroidUtilities.dp(20), AndroidUtilities.dp(12))

            header = LinearLayout(activity)
            header.setOrientation(LinearLayout.HORIZONTAL)
            header.setGravity(16)
            root.addView(header)
            title = TextView(activity)
            title.setText(self._tr("Редактор ответа", "Answer editor"))
            title.setTextSize(17)
            title.setTextColor(inactive)
            title.setTypeface(AndroidUtilities.bold())
            header.addView(title, LinearLayout.LayoutParams(0, -2, 1.0))

            panel_bg = 0x14000000
            icon_color = accent
            sel_color = 0x19000000
            try:
                panel_bg = Theme.getColor(Theme.key_chat_messagePanelBackground)
            except Exception:
                pass
            try:
                icon_color = Theme.getColor(Theme.key_chat_messagePanelIcons)
            except Exception:
                pass
            try:
                sel_color = Theme.getColor(Theme.key_listSelectorSDK21)
            except Exception:
                pass

            fmt_col = LinearLayout(activity)
            fmt_col.setOrientation(LinearLayout.VERTICAL)
            gd = GradientDrawable()
            gd.setColor(panel_bg)
            gd.setCornerRadius(AndroidUtilities.dp(14))
            fmt_col.setBackground(gd)
            fmt_lp = LinearLayout.LayoutParams(-1, -2)
            fmt_lp.bottomMargin = AndroidUtilities.dp(12)
            root.addView(fmt_col, fmt_lp)

            buttons = []

            def refresh():
                flags_state, quote_state = self._fmt_state(editor_field)
                for flag, btn in buttons:
                    if flag == "quote":
                        on = quote_state
                    elif flag == "regular":
                        on = (flags_state == 0 and not quote_state)
                    else:
                        on = bool(flags_state & flag)
                    btn.setTextColor(accent if on else icon_color)

            def make_fmt_btn(label, flag):
                btn = TextView(activity)
                btn.setText(label)
                btn.setTextSize(14)
                btn.setGravity(16 | 3)
                btn.setPadding(AndroidUtilities.dp(12), 0, 0, 0)
                btn.setTextColor(icon_color)
                btn.setBackground(Theme.createSelectorDrawable(sel_color, 1))
                btn.setOnClickListener(OnClickListener(
                    lambda v, fl=flag: (
                        me._clear_selection_format(editor_field) if fl == "regular"
                        else me._toggle_format(editor_field, fl), refresh())))
                buttons.append((flag, btn))
                return btn

            fmt_items = [
                (self._tr("Жирный", "Bold"), STYLE_BOLD),
                (self._tr("Курсив", "Italic"), STYLE_ITALIC),
                (self._tr("Моно", "Mono"), STYLE_MONO),
                (self._tr("Зачёрк.", "Strike"), STYLE_STRIKE),
                (self._tr("Подчёрк.", "Underline"), STYLE_UNDERLINE),
                (self._tr("Спойлер", "Spoiler"), 256),
                (self._tr("Цитата", "Quote"), "quote"),
                (self._tr("Сброс", "Reset"), "regular"),
            ]
            for i in range(0, len(fmt_items), 2):
                row = LinearLayout(activity)
                row.setOrientation(LinearLayout.HORIZONTAL)
                row.setGravity(16)
                fmt_col.addView(row, LinearLayout.LayoutParams(-1, AndroidUtilities.dp(38)))
                btn1 = make_fmt_btn(fmt_items[i][0], fmt_items[i][1])
                row.addView(btn1, LinearLayout.LayoutParams(0, -1, 1.0))
                if i + 1 < len(fmt_items):
                    btn2 = make_fmt_btn(fmt_items[i + 1][0], fmt_items[i + 1][1])
                    row.addView(btn2, LinearLayout.LayoutParams(0, -1, 1.0))

            editor_field = EditText(activity)
            editor_field.setSingleLine(False)
            editor_field.setMaxLines(14)
            editor_field.setGravity(51)
            editor_field.setTextSize(16)
            editor_field.setTextColor(inactive)
            editor_field.setHint(self._tr("Текст ответа…", "Answer text…"))
            editor_field.setHintTextColor(hint)
            bg = GradientDrawable()
            bg.setColor(Theme.getColor(Theme.key_chat_messagePanelBackground))
            bg.setCornerRadius(AndroidUtilities.dp(14))
            bg.setStroke(AndroidUtilities.dp(1), Theme.getColor(Theme.key_chat_messagePanelShadow))
            editor_field.setBackground(bg)
            editor_field.setPadding(AndroidUtilities.dp(16), AndroidUtilities.dp(12),
                                    AndroidUtilities.dp(16), AndroidUtilities.dp(12))
            editor_field.setText(src_text)
            if src_runs or src_quotes:
                self._apply_runs(editor_field.getText(), src_runs, src_quotes)
            root.addView(editor_field, LinearLayout.LayoutParams(-1, 0, 1.0))

            btn_row = LinearLayout(activity)
            btn_row.setOrientation(LinearLayout.HORIZONTAL)
            btn_row.setPadding(0, AndroidUtilities.dp(12), 0, 0)
            root.addView(btn_row)

            editor_sheet = [None]

            def on_done():
                try:
                    ed = editor_field.getText()
                    new_text = str(ed)
                    runs, quotes = self._collect_runs(ed)
                    answer_field.setText(new_text)
                    self._apply_runs(answer_field.getText(), runs, quotes)
                except Exception as e:
                    self.log("fast_answers: editor save error: " + repr(e))
                try:
                    editor_sheet[0].dismiss()
                except Exception:
                    pass

            def on_cancel():
                try:
                    editor_sheet[0].dismiss()
                except Exception:
                    pass

            cancel_btn = TextView(activity)
            cancel_btn.setText(self._tr("Отмена", "Cancel"))
            cancel_btn.setTextSize(15)
            cancel_btn.setTextColor(hint)
            cancel_btn.setPadding(0, AndroidUtilities.dp(12), 0, AndroidUtilities.dp(12))
            btn_row.addView(cancel_btn, LinearLayout.LayoutParams(0, -2, 1.0))
            cancel_btn.setOnClickListener(OnClickListener(lambda v: on_cancel()))

            save_btn = TextView(activity)
            save_btn.setText(self._tr("Сохранить", "Save"))
            save_btn.setTextSize(15)
            save_btn.setTextColor(accent)
            save_btn.setTypeface(AndroidUtilities.bold())
            save_btn.setGravity(5)
            save_btn.setPadding(0, AndroidUtilities.dp(12), 0, AndroidUtilities.dp(12))
            btn_row.addView(save_btn, LinearLayout.LayoutParams(0, -2, 1.0))
            save_btn.setOnClickListener(OnClickListener(lambda v: on_done()))

            refresh()

            sheet = Dialog(activity)
            editor_sheet[0] = sheet
            sheet.setContentView(root)
            try:
                w = sheet.getWindow()
                wlp = w.getAttributes()
                wlp.gravity = Gravity.TOP | Gravity.START
                wlp.width = WindowManager.LayoutParams.MATCH_PARENT
                wlp.height = WindowManager.LayoutParams.MATCH_PARENT
                w.setAttributes(wlp)
                try:
                    w.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                sheet.getWindow().setBackgroundDrawable(ColorDrawable(0))
            except Exception:
                pass
            sheet.show()
        except Exception as e:
            me.log("fast_answers: format editor error: " + repr(e))
            try:
                me.log(traceback.format_exc())
            except Exception:
                pass

    def _union_range(self, editable, start, end, span_cls):
        low = start
        high = end
        for sp in editable.getSpans(start, end, span_cls):
            ss = editable.getSpanStart(sp)
            se = editable.getSpanEnd(sp)
            if se is None or ss is None:
                continue
            ss = max(0, ss)
            se = min(editable.length(), se)
            if se > ss:
                low = min(low, ss)
                high = max(high, se)
        return low, high

    def _toggle_format(self, field, flag):
        try:
            editable = field.getText()
            total = editable.length()
            s = field.getSelectionStart()
            e = field.getSelectionEnd()
            if s is None:
                s = 0
            if e is None:
                e = total
            if s > e:
                s, e = e, s
            s = max(0, min(s, total))
            e = max(0, min(e, total))
            if e <= s:
                s, e = 0, total
            if flag == "quote":
                low, high = self._union_range(editable, s, e, QuoteSpan)
                spans = editable.getSpans(s, e, QuoteSpan)
                if spans:
                    for sp in list(spans):
                        editable.removeSpan(sp)
                else:
                    editable.setSpan(QuoteSpan(False, False, None), low, high, SPAN_EXCLUSIVE)
                return
            spans = editable.getSpans(s, e, TextStyleSpan)
            runs = []
            for sp in list(spans):
                ss = editable.getSpanStart(sp)
                se = editable.getSpanEnd(sp)
                if se is None or ss is None:
                    continue
                runs.append((max(0, ss), min(total, se), sp.getStyleFlags()))
                editable.removeSpan(sp)
            has_flag = False
            for _ss, _se, _fl in runs:
                if _fl & flag:
                    has_flag = True
                    break
            low = s
            high = e
            for _ss, _se, _fl in runs:
                low = min(low, _ss)
                high = max(high, _se)
            low = max(0, low)
            high = min(total, high)
            flags = 0
            for _ss, _se, _fl in runs:
                flags |= _fl
            if has_flag:
                flags = flags & ~flag
            else:
                flags = flags | flag
            if flags:
                try:
                    run = TextStyleSpan.TextStyleRun()
                    run.flags = flags
                    editable.setSpan(TextStyleSpan(run), low, high, SPAN_EXCLUSIVE)
                except Exception:
                    pass
        except Exception as e:
            self.log("fast_answers: toggle_format error: " + repr(e))

    def _collect_runs(self, editable):
        runs = []
        quotes = []
        try:
            total = editable.length()
            for sp in editable.getSpans(0, total, TextStyleSpan):
                s = editable.getSpanStart(sp)
                e = editable.getSpanEnd(sp)
                fl = sp.getStyleFlags()
                if s is not None and e is not None and e > s and fl:
                    runs.append([max(0, s), min(total, e), fl])
            for sp in editable.getSpans(0, total, QuoteSpan):
                s = editable.getSpanStart(sp)
                e = editable.getSpanEnd(sp)
                if s is not None and e is not None and e > s:
                    quotes.append([max(0, s), min(total, e)])
        except Exception:
            pass
        return runs, quotes

    def _apply_runs(self, editable, runs, quotes):
        try:
            total = editable.length()
            for item in (runs or []):
                try:
                    s = int(item[0])
                    e = int(item[1])
                    fl = int(item[2])
                    s = max(0, min(s, total))
                    e = max(0, min(e, total))
                    if e <= s:
                        continue
                    run = TextStyleSpan.TextStyleRun()
                    run.flags = fl
                    editable.setSpan(TextStyleSpan(run), s, e, SPAN_EXCLUSIVE)
                except Exception:
                    pass
            for item in (quotes or []):
                try:
                    s = int(item[0])
                    e = int(item[1])
                    s = max(0, min(s, total))
                    e = max(0, min(e, total))
                    if e <= s:
                        continue
                    editable.setSpan(QuoteSpan(False, False, None), s, e, SPAN_EXCLUSIVE)
                except Exception:
                    pass
        except Exception as e:
            self.log("fast_answers: apply_runs error: " + repr(e))

    def apply_styles_to_input(self, enter_view, payload):
        try:
            field = get_private_field(enter_view, "messageEditText")
            if field is None:
                return
            editable = field.getText()
            self._apply_runs(editable, payload.get("f"), payload.get("q"))
        except Exception as e:
            self.log("fast_answers: apply_styles_to_input error: " + repr(e))

    def send_now(self, chat_activity, enter_view):
        try:
            m = enter_view.getClass().getDeclaredMethod("sendMessage")
            m.setAccessible(True)
            m.invoke(enter_view)
            return
        except Exception:
            pass
        try:
            sb = enter_view.getSendButton()
            if sb is not None:
                sb.performClick()
        except Exception as e:
            self.log("fast_answers: send_now error: " + repr(e))

    # ------------------------------------------------------------
    # matching & panel injection
    # ------------------------------------------------------------

    def find_answers(self, query: str):
        q = (query or "").strip().lower()
        limit = self._min_len()
        if not q or len(q) < limit or len(q) > MAX_QUERY_LEN:
            return []
        if q[0] in "@#/:":
            return []
        search_text = bool(self.get_setting(TEXT_SEARCH_KEY, False))
        out = []
        seen = set()
        for entry in self._entries:
            answer = entry["a"]
            if answer in seen:
                continue
            matched = False
            for kw in (entry.get("k") or []):
                if kw.startswith(q):
                    matched = True
                    break
            if not matched and search_text:
                matched = q in answer.lower()
            if matched:
                seen.add(answer)
                payload = {"a": answer}
                if entry.get("f"):
                    payload["f"] = entry["f"]
                if entry.get("q"):
                    payload["q"] = entry["q"]
                out.append((answer, ", ".join(entry.get("k") or []), payload))
        return out

    def inject_answers(self, answers, adapter):
        fake_bot = TLRPC.TL_user()
        fake_bot.id = 987654321
        fake_bot.username = "fast_answers"
        fake_bot.first_name = "Fast Answers"
        fake_bot.bot = True

        results = ArrayList()
        self._answer_by_id.clear()
        for answer, keywords, payload in answers:
            self._seq += 1
            res_id = ID_PREFIX + str(self._seq)
            self._answer_by_id[res_id] = payload
            res = TLRPC.TL_botInlineResult()
            res.id = res_id
            res.type = "article"
            res.title = answer
            res.description = keywords
            res.send_message = TLRPC.TL_botInlineMessageText()
            res.send_message.message = answer
            res.send_message.no_webpage = True
            results.add(res)

        try:
            set_private_field(adapter, "foundContextBot", fake_bot)
            set_private_field(adapter, "searchResultBotContext", results)
        except Exception:
            return
        for field in ("searchResultBotContextSwitch", "searchResultBotWebViewSwitch",
                      "hintHashtag", "lastText"):
            try:
                set_private_field(adapter, field, None)
            except Exception:
                pass
        try:
            adapter.notifyDataSetChanged()
        except Exception:
            pass
        try:
            delegate = get_private_field(adapter, "delegate")
            if delegate is not None:
                try:
                    method = delegate.getClass().getDeclaredMethod("needChangePanelVisibility", Boolean.TYPE)
                except Exception:
                    method = delegate.getClass().getMethod("needChangePanelVisibility", Boolean.TYPE)
                method.setAccessible(True)
                method.invoke(delegate, Boolean(True))
        except Exception:
            pass

    def pop_answer(self, res_id: str):
        return self._answer_by_id.pop(res_id, None)

# __DEX_BEGIN__
# PLACEHOLDER
# __DEX_END__