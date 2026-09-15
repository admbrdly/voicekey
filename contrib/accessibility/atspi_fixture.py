"""Disposable text fields used only by atspi_probe.py --self-test."""
import sys
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

windows = []
for suffix in ('A', 'B'):
    window = Gtk.Window(title=sys.argv[1]+' '+suffix)
    window.set_default_size(420, 100)
    entry = Gtk.Entry()
    entry.set_text('α original field')
    entry.get_accessible().set_name(sys.argv[1]+' '+suffix)
    window.add(entry)
    window.show_all()
    windows.append(window)
Gtk.main()
