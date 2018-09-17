#!/usr/bin/env python
# coding: utf8

import sys, os, importlib, itertools, locale, io, subprocess, shutil, urllib2, yaml
from PyQt4 import QtCore, QtGui, QtWebKit
from PyQt4.QtGui import QDesktopServices

VERSION = '0.1'

sys_enc = locale.getpreferredencoding()
script_dir = os.path.dirname(os.path.realpath(__file__))
script_dir = script_dir.decode(sys_enc)
stylesheet_dir = os.path.join(script_dir, 'stylesheets/')

class App(QtGui.QMainWindow):

    @property
    def QSETTINGS(self):
        return QtCore.QSettings(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, 'MDviewer', 'MDviewer')

    def set_window_title(self):
        fpath, fname = os.path.split(os.path.abspath(self.filename))
        self.setWindowTitle(u'%s – MDviewer' % (fname))

    def set_env (self):
        fpath, fname = os.path.split(os.path.abspath(self.filename))
        fext = fname.split('.')[-1].lower()
        os.environ["MDVIEWER_EXT"] = fext
        os.environ["MDVIEWER_FILE"] = fname
        os.environ["MDVIEWER_ORIGIN"] = fpath

    def __init__(self, parent=None, filename=''):
        QtGui.QMainWindow.__init__(self, parent)
        self.filename = filename or os.path.join(script_dir, u'README.md')

        self.set_env()
        Settings.print_path()

        # Configure window
        self.set_window_title()
        self.resize(self.QSETTINGS.value('size', QtCore.QSize(800, 800)).toSize())
        self.move(self.QSETTINGS.value('pos', QtCore.QPoint(50, 50)).toPoint())

        # Activate WebView
        self.web_view = QtWebKit.QWebView()
        self.setCentralWidget(self.web_view)

        # Configure and start file watcher thread
        self.thread1 = WatcherThread(self.filename)
        self.connect(self.thread1, QtCore.SIGNAL('update(QString,QString)'), self.update)
        self.watcher = QtCore.QFileSystemWatcher([self.filename])
        self.watcher.fileChanged.connect(self.thread1.run)
        self.thread1.start()

        # Update TOC and perform auto-scrolling
        self.web_view.loadFinished.connect(self.after_update)

        # Set GUI
        self.set_menus()
        self.set_search_panel()

    def update(self, text, warn):
        '''Update document view.'''

        # Set WebView attributes
        self.web_view.settings().setAttribute(QtWebKit.QWebSettings.JavascriptEnabled, True)
        self.web_view.settings().setAttribute(QtWebKit.QWebSettings.PluginsEnabled, True)
        self.web_view.settings().setAttribute(QtWebKit.QWebSettings.DeveloperExtrasEnabled, True)

        # Set link policy
        self.web_view.page().linkHovered.connect(lambda link: self.setToolTip(link))
        self.web_view.page().setLinkDelegationPolicy(QtWebKit.QWebPage.DelegateExternalLinks)

        # Save data for auto-scrolling
        prev_doc = self.web_view.page().currentFrame()
        self.prev_scroll = prev_doc.scrollPosition()
        self.prev_ast = self.create_doc_ast(prev_doc.documentElement())

        # Update document
        self.web_view.setHtml(text, baseUrl=QtCore.QUrl('file:///' + os.path.join(os.getcwd(), self.filename)))

        # Restore scroll position
        curr_doc = self.web_view.page().currentFrame()
        curr_doc.setScrollPosition(self.prev_scroll)

        # Display processor warnings, if any
        if warn:
            QtGui.QMessageBox.warning(self, 'Processor warning', warn)

    def create_doc_ast(self, parentElement):
        '''
        Create document AST.

        Create tree of QWebElements starting from parentElement:
        [Parent, [[child, [child_of_child, ...], ...]], ...]
        where Parent is the first child of parentElement.
        '''

        children = []
        element = parentElement.firstChild()

        # Recursively traverse the AST
        while not element.isNull():
            if not any(t for t in ('HEAD', 'META', 'TITLE', 'STYLE', 'SCRIPT', 'LINK') if t == element.tagName()):
                if not (element.styleProperty('display', 2) == 'inline'
                        or element.styleProperty('display', 2) == 'inline-block'
                        or element.styleProperty('display', 2) == 'inline-flex'
                        or element.styleProperty('display', 2) == 'inline-table'
                        or element.styleProperty('display', 2) == 'none'
                        or element.styleProperty('visibility', 2) == 'hidden'):
                    if element not in children:
                        further = self.create_doc_ast(element)
                        children.append([element, further])
            element = element.nextSibling()

        if children:
            return children

    def after_update(self):
        scroll_delay = Settings.get('scroll_delay', 1500)
        # Wait until all asycronous JavaScript actions are complete
        QtCore.QTimer.singleShot(scroll_delay, self._after_update)

    def _after_update(self):
        '''Update TOC and scroll to the first change.'''

        def compare(prev_ast, curr_ast, prev_len, curr_len, go):
            for i, (element, children) in enumerate(curr_ast):

                # Compare children recursively
                if children:
                    if prev_ast:

                        try:
                            prev_children = prev_ast[i][1]
                        except IndexError:
                            prev_children = None

                        prev_children_len = len(prev_children) if prev_children else 0
                        go = compare(prev_children, children, prev_children_len, len(children), go)

                        if go:
                            return go
                    else:
                        return children[0][0]

                if element.tagName() == 'BODY':
                    go = 0
                elif curr_len > prev_len and i + 1 > prev_len:
                    # Block(s) added at the end of document
                    go = 1
                elif curr_len < prev_len and i + 1 == curr_len:
                    # Block(s) removed from the end of document
                    go = 1
                elif element.tagName() == prev_ast[i][0].tagName():
                    if element.toInnerXml() != prev_ast[i][0].toInnerXml():
                        # Block content changed
                        go = 1
                else:
                    # Block changed
                    go = 1
                if go:
                    value = element
                    break
                elif not go and i + 1 == curr_len:
                    # No actual changes
                    value = 0

            return value

        self.curr_doc = self.web_view.page().currentFrame()
        curr_ast = self.create_doc_ast(self.curr_doc.documentElement())
        prev_len, curr_len, go = len(self.prev_ast), len(curr_ast), 0

        # Refresh TOC
        self.generate_toc(curr_ast)

        # Scroll to the first change
        self._scroll(element=compare(self.prev_ast, curr_ast, prev_len, curr_len, go))

    def _scroll(self, element=0):
        '''Scroll to top of the element.'''

        if element:
            self.anim = QtCore.QPropertyAnimation(self.curr_doc, 'scrollPosition')
            start = self.curr_doc.scrollPosition()

            # Scroll to top of the element
            self.anim.setDuration(500)
            self.anim.setStartValue(QtCore.QPoint(start))
            self.anim.setEndValue(QtCore.QPoint(0, element.geometry().top()))
            self.anim.start()

            # Highlight the element via CSS property
            QtCore.QTimer.singleShot(500,  lambda: element.addClass('firstdiff-start'))
            QtCore.QTimer.singleShot(1000, lambda: element.addClass('firstdiff-end'))
            QtCore.QTimer.singleShot(1500, lambda: element.removeClass('firstdiff-start'))
            QtCore.QTimer.singleShot(2200, lambda: element.removeClass('firstdiff-end'))

    def generate_toc(self, curr_ast):

        def flatten(ls):
            for item in ls:
                if not item: continue
                if isinstance(item, list):
                    for x in flatten(item):
                        yield x
                else:
                    yield item

        self.toc_menu.clear()
        self.toc_menu.setDisabled(True)

        headers = []

        for element in flatten(curr_ast):
            if any(t for t in ('H1', 'H2', 'H3', 'H4', 'H5', 'H6') if t == element.tagName()):
                headers.append(element)

        for n, h in enumerate(headers, start=1):
            try:
                indent = int(h.tagName()[1:]) - 1
            except ValueError:
                break
            else:
                self.toc_menu.setDisabled(False)

            header = u'    '*indent + h.toPlainText().replace("&", "&&")
            vars(self)['toc_nav%d'%n] = QtGui.QAction(header, self)
            vars(self)['toc_nav%d'%n].triggered[()].connect(lambda header=h: self._scroll(header))
            self.toc_menu.addAction(vars(self)['toc_nav%d'%n])

    @staticmethod
    def set_stylesheet(self, stylesheet='default.css'):
        full_path = os.path.join(stylesheet_dir, stylesheet)
        url = QtCore.QUrl.fromLocalFile(full_path)
        self.web_view.settings().setUserStyleSheetUrl(url)

    def handle_link_clicked(self, url):
        QDesktopServices.openUrl(url)

    def open_file(self):
        filename = unicode(QtGui.QFileDialog.getOpenFileName(self, 'Open File', os.path.dirname(self.filename)))
        if filename != '':
            self.filename = self.thread1.filename = filename
            self.set_window_title()
            self.thread1.run()
        else:
            pass

    def save_html(self):
        filename = unicode(QtGui.QFileDialog.getSaveFileName(self, 'Save File', os.path.dirname(self.filename)))
        if filename != '':
            path = Settings.get('processor_path', 'pandoc')
            args = Settings.get('processor_args', '--from=markdown --to=html5 --standalone')
            args = ('%s' % (args)).split() + [self.filename]
            caller = QtCore.QProcess()
            caller.start(path, args)
            caller.waitForFinished()
            html = unicode(caller.readAllStandardOutput(), 'utf8')
            with io.open(filename, 'w', encoding='utf8') as f:
                f.writelines(unicode(html))
                f.close()
        else:
            pass

    def show_search_panel(self):
        self.addToolBar(0x8, self.search_bar)
        self.search_bar.show()
        self.field.setFocus()
        self.field.selectAll()

    def find(self, text, btn=''):
        p = self.web_view.page()
        back = p.FindFlags(1) if btn is self.prev else p.FindFlags(0)
        case = p.FindFlags(2) if self.case.isChecked() else p.FindFlags(0)
        wrap = p.FindFlags(4) if self.wrap.isChecked() else p.FindFlags(0)
        p.findText('', p.FindFlags(8))
        p.findText(text, back | wrap | case)

    def about(self):
        msg_about = QtGui.QMessageBox(0, 'About MDviewer', u'Version: %s' % (VERSION), parent=self)
        msg_about.show()

    def set_menus(self):

        menubar = self.menuBar()

        file_menu = menubar.addMenu('&File')

        for d in (
                {'label': u'&Open...',      'keys': 'Ctrl+O', 'func': self.open_file},
                {'label': u'&Save HTML...', 'keys': 'Ctrl+S', 'func': self.save_html},
                {'label': u'&Find...',      'keys': 'Ctrl+F', 'func': self.show_search_panel},
                {'label': u'&Print...',     'keys': 'Ctrl+P', 'func': self.print_doc},
                {'label': u'&Quit',         'keys': 'Ctrl+Q', 'func': self.quit}
                 ):
            action = QtGui.QAction(d['label'], self)
            action.setShortcut(d['keys'])
            action.triggered.connect(d['func'])
            file_menu.addAction(action)

        view_menu = menubar.addMenu("&View")

        for d in (
                {'label': u'Zoom &In',     'keys': 'Ctrl++', 'func': lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor()+.1)},
                {'label': u'Zoom &Out',    'keys': 'Ctrl+-', 'func': lambda: self.web_view.setZoomFactor(self.web_view.zoomFactor()-.1)},
                {'label': u'&Actual Size', 'keys': 'Ctrl+=', 'func': lambda: self.web_view.setZoomFactor(1)}
                 ):
            action = QtGui.QAction(d['label'], self)
            action.setShortcut(d['keys'])
            action.triggered.connect(d['func'])
            view_menu.addAction(action)

        if os.path.exists(stylesheet_dir):
            default = ''
            sheets = []
            for f in os.listdir(stylesheet_dir):
                if not f.endswith('.css'): continue
                sheets.append(QtGui.QAction(f, self))
                if len(sheets) < 10:
                    sheets[-1].setShortcut('Ctrl+%d' % len(sheets))
                sheets[-1].triggered.connect(
                    lambda x, stylesheet=f: self.set_stylesheet(self, stylesheet))
            style_menu = menubar.addMenu('&Style')
            for item in sheets:
                style_menu.addAction(item)
            self.set_stylesheet(self, 'default.css')

        self.toc_menu = menubar.addMenu('&Goto')
        self.toc_menu.setStyleSheet('menu-scrollable: 1')
        self.toc_menu.setDisabled(True)

        help_menu = menubar.addMenu("&Help")

        for d in (
                {'label': u'About...', 'func': self.about},
                 ):
            action = QtGui.QAction(d['label'], self)
            action.triggered.connect(d['func'])
            help_menu.addAction(action)

        # Redefine context menu for reloading
        reload_action = self.web_view.page().action(QtWebKit.QWebPage.Reload)
        reload_action.setShortcut(QtGui.QKeySequence.Refresh)
        reload_action.triggered.connect(self.thread1.run)
        self.web_view.addAction(reload_action)

    def set_search_panel(self):
        self.search_bar = QtGui.QToolBar()

        # Define buttons
        self.done = QtGui.QPushButton(u'Done', self)
        self.case = QtGui.QPushButton(u'Case', self)
        self.wrap = QtGui.QPushButton(u'Wrap', self)
        self.next = QtGui.QPushButton(u'Next', self)
        self.prev = QtGui.QPushButton(u'Previous', self)

        # Define text field
        class DUMB(QtGui.QLineEdit): pass
        self.field = DUMB()

        # Restart search at button toggling
        def _toggle_btn(btn=''):
            self.field.setFocus()
            self.find(self.field.text(), btn)

        # Hide search panel
        def _escape():
            if self.search_bar.isVisible():
                self.search_bar.hide()

        # Add wigets to search panel
        for w in (self.done, self.case, self.wrap, self.field, self.next, self.prev):
            self.search_bar.addWidget(w)
            if type(w) == QtGui.QPushButton:
                w.setFlat(False)
                if any(t for t in (self.case, self.wrap) if t is w):
                    w.setCheckable(True)
                    w.clicked.connect(_toggle_btn)
                if any(t for t in (self.next, self.prev) if t is w):
                    w.pressed[()].connect(lambda btn=w: _toggle_btn(btn))
        self.done.pressed.connect(_escape)

        # Activate incremental search
        self.field.textChanged.connect(self.find)

    def print_doc(self):
        dialog = QtGui.QPrintPreviewDialog()
        dialog.paintRequested.connect(self.web_view.print_)
        dialog.exec_()

    def quit(self, QCloseEvent):

        # Save settings
        self.QSETTINGS.setValue('size', self.size())
        self.QSETTINGS.setValue('pos', self.pos())

        QtGui.qApp.quit()

class WatcherThread(QtCore.QThread):

    def __init__(self, filename):
        QtCore.QThread.__init__(self)
        self.filename = filename
        # print os.getenv('MDVIEWER_EXT')
        # print os.getenv('MDVIEWER_FILE')
        # print os.getenv('MDVIEWER_ORIGIN')

    def run(self):
        warn = ''
        html, warn = self.processor_rules()
        self.emit(QtCore.SIGNAL('update(QString,QString)'), html, warn)

    def processor_rules(self):
        path = Settings.get('processor_path', 'pandoc')
        args = Settings.get('processor_args', '--from=markdown --to=html5 --standalone')
        args = ('%s' % (args)).split() + [self.filename]
        caller = QtCore.QProcess()
        # status = caller.execute(path, args)
        caller.start(path, args)
        caller.waitForFinished()
        html = unicode(caller.readAllStandardOutput(), 'utf8')
        warn = unicode(caller.readAllStandardError(), 'utf8')
        return (html, warn)

class Settings:
    def __init__(self):
        if os.name == 'nt':
            self.user_source = os.path.join(os.getenv('APPDATA'), 'mdviewer/settings.yml')
        else:
            self.user_source = os.path.join(os.getenv('HOME'), '.config/mdviewer/settings.yml')
        self.app_source = os.path.join(script_dir, 'settings.yml')
        self.settings_file = self.user_source if os.path.exists(self.user_source) else self.app_source
        self.reload_settings()

    def reload_settings(self):
        with io.open(self.settings_file, 'r', encoding='utf8') as f:
            self.settings = yaml.safe_load(f)

    @classmethod
    def get(cls, key, default_value):
        return cls().settings.get(key, default_value)

    @classmethod
    def print_path(cls):
        print 'Settings: %s' % cls().settings_file

def main():
    app = QtGui.QApplication(sys.argv)
    if len(sys.argv) != 2:
        window = App()
    else:
        window = App(filename=sys.argv[1].decode(sys_enc))
    window.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()

