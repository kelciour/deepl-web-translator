import html
import itertools
import os
import re
import time
import traceback
import urllib

from anki.hooks import addHook
from aqt import mw
from aqt.qt import *
from aqt.utils import showInfo, showText, showWarning, tooltip
from bs4 import BeautifulSoup
from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QIcon
from PyQt5.QtWebEngineCore import QWebEngineUrlRequestInterceptor
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
)

from . import form, lang


class TooManyRequestsException(Exception):
    pass


class UnknownException(Exception):
    pass


class WebEngineUrlRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self):
        super().__init__()
        self.lastUpdate = None
        self.translating = False
        self.count = 0

    def interceptRequest(self, info):
        url = info.requestUrl().toString()
        if "/jsonrpc" in url:
            info.setHttpHeader(b"referer", b"https://www.deepl.com/")
            info.setHttpHeader(b"accept-language", b"en-US,en;q=0.9")
        self.lastUpdate = time.time()
        if self.translating:
            self.count += 1


class WebEnginePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, sourceID):
        pass


class DeepLTranslatorHelper(QDialog):
    def __init__(self, txt, sourceLangCode, targetLangCode, browser):
        QDialog.__init__(self, browser)
        self.start = False
        self.finish = False
        self.ready = None
        self.text = txt + "\n~~~~~"
        self.sourceLangCode = sourceLangCode
        self.targetLangCode = targetLangCode
        self.translation = ""
        self.startTime = time.time()
        self.exception = None
        self.browser = browser
        self.initUI()

    def initUI(self):
        self.webEngineView = QWebEngineView(self)
        self.webEnginePage = WebEnginePage(self.webEngineView)
        self.webEngineView.setPage(self.webEnginePage)

        layout = QVBoxLayout()
        layout.addWidget(self.webEngineView)
        self.setLayout(layout)

        self.webEngineView.page().urlChanged.connect(self.onLoadFinished)

        self.interceptor = WebEngineUrlRequestInterceptor()
        self.profile = self.webEngineView.page().profile()
        self.profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.96 Safari/537.36"
        )
        self.profile.setUrlRequestInterceptor(self.interceptor)

        if self.sourceLangCode == "auto":
            deepl_translator_url = f"https://www.deepl.com/en/translator"
        else:
            deepl_translator_url = f"https://www.deepl.com/en/translator#{self.sourceLangCode}/{self.targetLangCode}/"

        self.setWindowTitle("DeepL Web Translator")

        self.setWindowState(Qt.WindowMinimized)
        self.setWindowOpacity(0)

        self.show()

        self.webEngineView.load(QUrl(deepl_translator_url))

    def sleep(self, seconds):
        start = time.time()
        while time.time() - start < seconds:
            time.sleep(0.01)
            QApplication.instance().processEvents()

    def onLoadFinished(self):
        if not self.start:
            self.start = True
            self.updateReadyState()

    def onReadyState(self, state):
        if state != "complete":
            return QTimer.singleShot(250, self.updateReadyState)
        self.updateTranslatorState()

    def updateTranslatorState(self):
        self.webEngineView.page().runJavaScript(
            """
(function () {{
lmt_source_textarea_placeholder = document.querySelector('#dl_translator .lmt__textarea_placeholder_text:not(.dl_hidden)');
if (lmt_source_textarea_placeholder === null) return null;
lmt_target_textarea = document.querySelector('#dl_translator .lmt__target_textarea');
if (lmt_target_textarea === null) return null;
sourceLangCode = "{}";
if (sourceLangCode === "auto") {{
    document.querySelector('.lmt__language_select--target .lmt__language_select__active').click();
    document.querySelector('.lmt__language_select__menu [dl-test^="translator-lang-option-{}"]').click();
}}
return lmt_source_textarea_placeholder.getAttribute('style') !== null && lmt_target_textarea.getAttribute('lang').startsWith('{}');
}})();
            """.format(
                self.sourceLangCode, self.targetLangCode, self.targetLangCode
            ),
            self.onTranslatorReady,
        )

    def onTranslatorReady(self, state):
        if not state:
            if time.time() - self.startTime > 15:
                self.exception = UnknownException()
                self.done(1)
                return
            return QTimer.singleShot(50, self.updateTranslatorState)
        return QTimer.singleShot(50, self.translateText)

    def updateReadyState(self):
        self.webEngineView.page().runJavaScript(
            "document.readyState", self.onReadyState
        )

    def translateText(self):
        self.webEngineView.page().profile().cookieStore().deleteAllCookies()
        js = """(function () {{
translator = document.querySelector("#dl_translator textarea.lmt__source_textarea");
translator.value = `{}`;
translator.dispatchEvent(new Event('focus', {{ bubbles: true }}));
translator.dispatchEvent(new Event('change', {{ bubbles: true }}));
}})();
""".format(
            self.text
        )
        self.webEngineView.page().runJavaScript(js)
        self.startTime = time.time()
        self.isTranslationReady()

    def getTranslation(self, translated):
        if translated is None:
            return QTimer.singleShot(50, self.isTranslationReady)
        translated = translated.rstrip()
        if self.translation != translated:
            self.startTime = time.time()
        self.translation = translated.strip()
        self.translation = html.unescape(self.translation)
        if not self.translation.endswith("~~~~~"):
            return QTimer.singleShot(50, self.isTranslationReady)
        self.translation = self.translation[:-5]
        self.finish = True
        self.done(0)

    def isTranslationReady(self):
        if time.time() - self.startTime > 30:
            self.exception = TooManyRequestsException()
            self.done(1)
            return
        js = """(function () {{
dummydiv = document.querySelector('#target-dummydiv')
if (dummydiv) return dummydiv.innerHTML;
}})()
        """
        self.webEngineView.page().runJavaScript(js, self.getTranslation)


class DeepLTranslator(QDialog):
    def __init__(self, browser, nids) -> None:
        QDialog.__init__(self, browser)
        self.browser = browser
        self.nids = nids
        self.total_count = 0
        self.exception = None

        self.form = form.Ui_Dialog()
        self.form.setupUi(self)

        self.sourceLanguages = {}
        for x in lang.source_languages:
            assert x["name"] not in self.sourceLanguages, x["name"]
            self.sourceLanguages[x["name"]] = x["code"]

        self.targetLanguages = {}
        for x in lang.target_languages:
            assert x["name"] not in self.targetLanguages, x["name"]
            self.targetLanguages[x["name"]] = x["code"]

        self.form.sourceLang.addItems(self.sourceLanguages)

        self.form.targetLang.addItems(self.targetLanguages)
        self.form.targetLang.setCurrentIndex(
            list(self.targetLanguages).index("English (US)")
        )

        def getLangCode(combobox, languages):
            text = combobox.currentText()
            if not text:
                return "##"
            return languages[text]

        def updateTargetLang():
            self.sourceLangCode = getLangCode(
                self.form.sourceLang, self.sourceLanguages
            )
            self.targetLangCode = getLangCode(
                self.form.targetLang, self.targetLanguages
            )
            if self.targetLangCode.startswith(self.sourceLangCode):
                self.form.targetLang.blockSignals(True)
                self.form.targetLang.setCurrentIndex(-1)
                self.form.targetLang.blockSignals(False)

        def updateSourceLang():
            self.sourceLangCode = getLangCode(
                self.form.sourceLang, self.sourceLanguages
            )
            self.targetLangCode = getLangCode(
                self.form.targetLang, self.targetLanguages
            )
            if self.targetLangCode.startswith(self.sourceLangCode):
                self.form.sourceLang.blockSignals(True)
                self.form.sourceLang.setCurrentIndex(0)
                self.form.sourceLang.blockSignals(False)

        self.form.sourceLang.currentIndexChanged.connect(updateTargetLang)
        self.form.targetLang.currentIndexChanged.connect(updateSourceLang)

        note = mw.col.getNote(nids[0])
        fields = note.keys()

        self.form.sourceField.addItems(fields)
        self.form.sourceField.setCurrentIndex(1)

        self.form.targetField.addItems(fields)
        self.form.targetField.setCurrentIndex(len(fields) - 1)

        self.config = mw.addonManager.getConfig(__name__)

        for fld, cb in [
            ("Source Field", self.form.sourceField),
            ("Target Field", self.form.targetField),
        ]:
            if self.config[fld] and self.config[fld] in note:
                cb.setCurrentIndex(fields.index(self.config[fld]))

        for key, cb in [
            ("Source Language", self.form.sourceLang),
            ("Target Language", self.form.targetLang),
        ]:
            if self.config[key]:
                cb.setCurrentIndex(cb.findText(self.config[key]))

        self.form.checkBoxOverwrite.setChecked(self.config["Overwrite"])

        self.icon = os.path.join(os.path.dirname(__file__), "favicon.png")
        self.setWindowIcon(QIcon(self.icon))

        self.show()

    def chunkify(self):
        chunk = {"nids": [], "query": "", "progress": 0}
        for nid in self.nids:
            note = mw.col.getNote(nid)
            chunk["progress"] += 1
            if not note[self.sourceField]:
                continue
            if self.sourceField not in note:
                continue
            if self.targetField not in note:
                continue
            if note[self.targetField] and not self.config["Overwrite"]:
                continue
            soup = BeautifulSoup(note[self.sourceField], "html.parser")
            text = soup.get_text()
            text = re.sub(
                r"{{c(\d+)::(.*?)(::.*?)?}}", r"<c\1>\2</c>", text, flags=re.I
            )
            self.total_count += len(text)
            if not chunk["nids"]:
                chunk["nids"].append(nid)
                chunk["query"] += text
            elif len(chunk["query"] + text) < 4750:
                chunk["nids"].append(nid)
                chunk["query"] += "\n~~~\n" + text
            else:
                yield chunk
                chunk = {"nids": [nid], "query": text, "progress": chunk["progress"]}
        if chunk["nids"]:
            yield chunk

    def sleep(self, seconds):
        start = time.time()
        while time.time() - start < seconds:
            time.sleep(0.01)
            QApplication.instance().processEvents()

    def translate(self, query):
        ex = DeepLTranslatorHelper(
            query, self.sourceLangCode, self.targetLangCode, self.browser
        )
        ex.exec_()
        if ex.exception:
            self.exception = ex.exception
            raise ex.exception
        return ex.translation

    def accept(self):
        self.sourceField = self.form.sourceField.currentText()
        self.targetField = self.form.targetField.currentText()

        self.config["Source Field"] = self.sourceField
        self.config["Target Field"] = self.targetField

        self.sourceLang = self.form.sourceLang.currentText()
        self.targetLang = self.form.targetLang.currentText()

        if not self.targetLang:
            showWarning("Select target language")
            return

        QDialog.accept(self)

        self.config["Source Language"] = self.sourceLang
        self.config["Target Language"] = self.targetLang

        self.config["Overwrite"] = self.form.checkBoxOverwrite.isChecked()

        mw.addonManager.writeConfig(__name__, self.config)

        self.sourceLangCode = self.sourceLanguages[self.sourceLang]
        self.targetLangCode = self.targetLanguages[self.targetLang]

        self.browser.mw.progress.start(parent=self.browser)
        self.browser.mw.progress._win.setWindowIcon(QIcon(self.icon))
        self.browser.mw.progress._win.setWindowTitle("DeepL Web Translator")

        error = None
        try:
            for num, chunk in enumerate(self.chunkify(), 1):
                if self.browser.mw.progress._win.wantCancel:
                    break
                if num % 15 == 0:
                    mx = 1 + (num / 15 - 1) % 3
                    self.sleep(30 * mx)
                elif num != 1:
                    self.sleep(10)

                nids = chunk["nids"]
                query = chunk["query"]

                attributes = {}
                idx = itertools.count(1)

                def attrs_to_i(m):
                    i = str(next(idx))
                    attributes[i] = m.group(2)
                    return "<{} i={}>".format(m.group(1), i)

                query = re.sub(r"<(\w+) ([^>]+)>", attrs_to_i, query)

                rows = query.split("\n~~~\n")
                assert len(nids) == len(
                    rows
                ), "Chunks: {} != {}\n\n-------------\n{}\n-------------\n".format(
                    len(nids), len(rows), urllib.parse.unquote(query)
                )

                data = self.translate(query)

                translated = data

                def i_to_attrs(m):
                    return "<{} {}>".format(m.group(1), attributes[m.group(2)])

                translated = re.sub(r"<(\w+) i\s*=\s*(\d+)>", i_to_attrs, translated)

                translated = re.split(r"\n[~〜]{3}\n", translated)
                assert len(nids) == len(
                    translated
                ), "Translated: {} notes != {}\n\n-------------\n{}\n-------------\n{}\n-------------\n".format(
                    len(nids), len(translated), urllib.parse.unquote(query), translated
                )

                for nid, text in zip(nids, translated):
                    note = mw.col.getNote(nid)
                    text = re.sub(r" (<c\d+>) ", r" \1", text)
                    text = re.sub(r" (</c\d+>) ", r"\1 ", text)
                    text = re.sub(r"<c(\d+)>(.*?)</c>", r"{{c\1::\2}}", text)
                    text = re.sub(r" }}([,.?!])", r"}}\1", text)
                    text = re.sub(r"{{c(\d+)::(.*?) +}} ", r"{{c\1::\2}} ", text)
                    text = re.sub(r" ([,:;!?])", r"\1", text)
                    text = text.replace("< ", "<")
                    text = text.strip()
                    note[self.targetField] = text
                    note.flush()

                self.browser.mw.progress.update(
                    "Processed {}/{} notes...".format(chunk["progress"], len(self.nids))
                )
        except TooManyRequestsException as e:
            self.exception = "TooManyRequests"
        except UnknownException as e:
            self.exception = "Unknown"
        except Exception as e:
            error = traceback.format_exc()
        finally:
            self.browser.mw.progress.finish()
            self.browser.mw.reset()
            mw.col.save()

        if self.exception is not None:
            if self.exception == "TooManyRequests":
                showWarning(
                    "Access temporarily suspended. "
                    "It appears that your network is sending too many requests to our servers. "
                    "Please try again later.",
                    title="DeepL Web Translator",
                    parent=self.browser,
                )
            else:
                assert self.exception == "Unknown"
                showWarning(
                    "Something went wrong.",
                    title="DeepL Web Translator",
                    parent=self.browser,
                )
        elif error:
            showText("Error:\n\n" + str(error), parent=self.browser)
        else:
            showInfo(
                "Processed {} notes.".format(len(self.nids)),
                parent=self.browser,
            )


def onDeepLTranslator(browser):
    nids = browser.selectedNotes()

    if not nids:
        return tooltip("No cards selected.")

    DeepLTranslator(browser, nids)


def setupMenu(browser):
    a = QAction("DeepL Web Translator", browser)
    a.triggered.connect(lambda: onDeepLTranslator(browser))
    browser.form.menuEdit.addSeparator()
    browser.form.menuEdit.addAction(a)


addHook("browser.setupMenus", setupMenu)
