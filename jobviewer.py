#!/usr/bin/env python

## Copyright (C) 2007, 2008 Tim Waugh <twaugh@redhat.com>
## Copyright (C) 2007, 2008 Red Hat, Inc.

## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

import authconn
import cups
import dbus
import dbus.glib
import dbus.service
import pynotify
import gettext
import gobject
import gtk
import gtk.glade
import pango
import pynotify
import sys
import time

from debug import *
import statereason
import pprint

from gettext import gettext as _
DOMAIN="system-config-printer"
gettext.textdomain (DOMAIN)
gtk.glade.bindtextdomain (DOMAIN)
from statereason import StateReason
statereason.set_gettext_function (_)

APPDIR="/usr/share/system-config-printer"
GLADE="applet.glade"
ICON="printer"
SEARCHING_ICON="document-print-preview"

CONNECTING_TIMEOUT = 60 # seconds
MIN_REFRESH_INTERVAL = 1 # seconds

def state_reason_is_harmless (reason):
    if (reason.startswith ("moving-to-paused") or
        reason.startswith ("paused") or
        reason.startswith ("shutdown") or
        reason.startswith ("stopping") or
        reason.startswith ("stopped-partly")):
        return True
    return False

def collect_printer_state_reasons (connection):
    result = {}
    printers = connection.getPrinters ()
    for name, printer in printers.iteritems ():
        reasons = printer["printer-state-reasons"]
        if type (reasons) != list:
            # Work around a bug that was fixed in pycups-1.9.20.
            reasons = [reasons]
        for reason in reasons:
            if reason == "none":
                break
            if state_reason_is_harmless (reason):
                continue
            if not result.has_key (name):
                result[name] = []
            result[name].append (StateReason (name, reason))
    return result

def worst_printer_state_reason (printer_reasons=None, connection=None):
    """Fetches the printer list and checks printer-state-reason for
    each printer, returning a StateReason for the most severe
    printer-state-reason, or None."""
    worst_reason = None

    if printer_reasons == None:
        if connection == None:
            try:
                connection = cups.Connection ()
            except:
                return None

        printer_reasons = collect_printer_state_reasons (connection)

    for printer, reasons in printer_reasons.iteritems ():
        for reason in reasons:
            if worst_reason == None:
                worst_reason = reason
                continue
            if reason > worst_reason:
                worst_reason = reason

    return worst_reason

class JobViewer:
    def __init__(self, bus=None, loop=None, service_running=False,
                 trayicon=False, suppress_icon_hide=False,
                 my_jobs=True, specific_dests=None):
        self.loop = loop
        self.service_running = service_running
        self.trayicon = trayicon
        self.suppress_icon_hide = suppress_icon_hide
        self.my_jobs = my_jobs
        self.specific_dests = specific_dests

        self.jobs = {}
        self.jobiters = {}
        self.which_jobs = "not-completed"
        self.printer_state_reasons = {}
        self.hidden = False
        self.connecting_to_device = {} # dict of printer->time first seen
        self.still_connecting = set()
        self.will_update_job_creation_times = False # whether timeout is set
        self.special_status_icon = False

        self.xml = gtk.glade.XML(APPDIR + "/" + GLADE, domain = DOMAIN)
        self.xml.signal_autoconnect(self)
        self.treeview = self.xml.get_widget ('treeview')
        text=0
        for name in [_("Job"),
                     _("Document"),
                     _("Printer"),
                     _("Size"),
                     _("Time submitted"),
                     _("Status")]:
            cell = gtk.CellRendererText()
            if text == 1 or text == 2:
                # Ellipsize the 'Document' and 'Printer' columns.
                cell.set_property ("ellipsize", pango.ELLIPSIZE_END)
                cell.set_property ("width-chars", 20)
            column = gtk.TreeViewColumn(name, cell, text=text)
            column.set_resizable(True)
            self.treeview.append_column(column)
            text += 1

        self.treeview.get_selection().set_mode(gtk.SELECTION_SINGLE)
        self.store = gtk.TreeStore(int, str, str, str, str, str)
        self.store.set_sort_column_id (0, gtk.SORT_DESCENDING)
        self.treeview.set_model(self.store)
        self.treeview.set_rules_hint (True)

        self.MainWindow = self.xml.get_widget ('MainWindow')
        self.MainWindow.set_icon_name (ICON)
        self.MainWindow.hide ()

        self.statusbar = self.xml.get_widget ('statusbar')
        self.statusbar_set = False
        self.reasons_seen = {}

        self.job_popupmenu = self.xml.get_widget ('job_popupmenu')
        self.icon_popupmenu = self.xml.get_widget ('icon_popupmenu')
        self.cancel = self.xml.get_widget ('cancel')
        self.hold = self.xml.get_widget ('hold')
        self.release = self.xml.get_widget ('release')
        self.reprint = self.xml.get_widget ('reprint')

        self.show_printer_status = self.xml.get_widget ('show_printer_status')
        self.PrintersWindow = self.xml.get_widget ('PrintersWindow')
        self.PrintersWindow.set_icon_name (ICON)
        self.PrintersWindow.hide ()
        self.treeview_printers = self.xml.get_widget ('treeview_printers')
        column = gtk.TreeViewColumn(_("Printer"))
        icon = gtk.CellRendererPixbuf()
        column.pack_start (icon, False)
        text = gtk.CellRendererText()
        column.set_resizable(True)
        column.pack_start (text, False)
        column.set_cell_data_func (icon, self.set_printer_status_icon)
        column.set_cell_data_func (text, self.set_printer_status_name)
        column.set_resizable (True)
        column.set_sort_column_id (1)
        column.set_sort_order (gtk.SORT_ASCENDING)
        self.treeview_printers.append_column(column)
        cell = gtk.CellRendererText()
        column = gtk.TreeViewColumn(_("Message"), cell, text=2)
        column.set_resizable(True)
        cell.set_property ("ellipsize", pango.ELLIPSIZE_END)
        self.treeview_printers.append_column(column)

        self.treeview_printers.get_selection().set_mode(gtk.SELECTION_NONE)
        self.store_printers = gtk.TreeStore (int, str, str)
        self.treeview_printers.set_model(self.store_printers)

        self.lblPasswordPrompt = self.xml.get_widget('lblPasswordPrompt')
        self.PasswordDialog = self.xml.get_widget('PasswordDialog')
        self.entPasswd = self.xml.get_widget('entPasswd')
        self.prompt_primary = self.lblPasswordPrompt.get_label ()
        self.lblError = self.xml.get_widget('lblError')
        self.ErrorDialog = self.xml.get_widget('ErrorDialog')

        cups.setPasswordCB(self.cupsPasswdCallback)

        if self.trayicon:
            self.statusicon = gtk.StatusIcon ()
            theme = gtk.icon_theme_get_default ()
            pixbuf = theme.load_icon (ICON, 22, 0)
            self.statusicon.set_from_pixbuf (pixbuf)
            self.icon_jobs = self.statusicon.get_pixbuf ()
            self.icon_no_jobs = self.icon_jobs.copy ()
            self.icon_no_jobs.fill (0)
            self.icon_jobs.composite (self.icon_no_jobs,
                                      0, 0,
                                      self.icon_no_jobs.get_width(),
                                      self.icon_no_jobs.get_height(),
                                      0, 0,
                                      1.0, 1.0,
                                      gtk.gdk.INTERP_BILINEAR,
                                      127)
            self.set_statusicon_from_pixbuf (self.icon_no_jobs)
            self.statusicon.connect ('activate', self.toggle_window_display)
            self.statusicon.connect ('popup-menu', self.on_icon_popupmenu)

            # We need the statusicon to actually get placed on the screen
            # in case refresh() wants to attach a notification to it.
            while gtk.events_pending ():
                gtk.main_iteration ()

            self.notify = None
            self.notified_reason = None

        # D-Bus
        if bus == None:
            bus = dbus.SystemBus ()

        bus.add_signal_receiver (self.handle_dbus_signal,
                                 path="/com/redhat/PrinterSpooler",
                                 dbus_interface="com.redhat.PrinterSpooler")

        self.sub_id = -1
        self.refresh ()

        if not self.trayicon:
            self.MainWindow.show ()

    def cleanup (self):
        if self.sub_id != -1:
            try:
                c = cups.Connection ()
                c.cancelSubscription (self.sub_id)
                debugprint ("Canceled subscription %d" % self.sub_id)
            except:
                pass

    # Handle "special" status icon
    def set_special_statusicon (self, iconname):
        self.special_status_icon = True
        self.statusicon.set_from_icon_name (iconname)
        self.set_statusicon_visibility ()

    def unset_special_statusicon (self):
        self.special_status_icon = False
        self.statusicon.set_from_pixbuf (self.saved_statusicon_pixbuf)

    def notify_new_printer (self, printer, notification):
        self.notify = notification
        self.notified_reason = StateReason (printer, "new-printer-report")
        notification.connect ('closed', self.on_notification_closed)
        self.hidden = False
        self.set_statusicon_visibility ()
        # Let the icon show itself, ready for the notification
        while gtk.events_pending ():
            gtk.main_iteration ()
        notification.attach_to_status_icon (jobmanager.statusicon)
        notification.show ()

    def set_statusicon_from_pixbuf (self, pb):
        self.saved_statusicon_pixbuf = pb
        if not self.special_status_icon:
            self.statusicon.set_from_pixbuf (pb)

    def on_delete_event(self, *args):
        if self.trayicon or not self.loop:
            self.MainWindow.hide ()
            if self.show_printer_status.get_active ():
                self.PrintersWindow.hide ()

            if not self.loop:
                self.cleanup ()
        else:
            self.loop.quit ()
        return True

    def on_printer_status_delete_event(self, *args):
        self.show_printer_status.set_active (False)
        self.PrintersWindow.hide()
        return True

    def cupsPasswdCallback(self, querystring):
        self.lblPasswordPrompt.set_label (self.prompt_primary + querystring)
        self.PasswordDialog.set_transient_for (self.MainWindow)
        self.entPasswd.grab_focus ()
        result = self.PasswordDialog.run()
        self.PasswordDialog.hide()
        if result == gtk.RESPONSE_OK:
            return self.entPasswd.get_text()
        return ''

    def show_IPP_Error(self, exception, message):
        if exception == cups.IPP_NOT_AUTHORIZED:
            error_text = ('<span weight="bold" size="larger">' +
                          _('Not authorized') + '</span>\n\n' +
                          _('The password may be incorrect.'))
        else:
            error_text = ('<span weight="bold" size="larger">' +
                          _('CUPS server error') + '</span>\n\n' +
                          _("There was an error during the CUPS "\
                            "operation: '%s'.")) % message
        self.lblError.set_markup(error_text)
        self.ErrorDialog.set_transient_for (self.MainWindow)
        self.ErrorDialog.run()
        self.ErrorDialog.hide()

    def toggle_window_display(self, icon, force_show=False):
        visible = self.MainWindow.get_property('visible')
        if force_show:
            visible = False

        if visible:
            self.MainWindow.hide()
            if self.show_printer_status.get_active ():
                self.PrintersWindow.hide()
        else:
            self.MainWindow.show()
            if self.show_printer_status.get_active ():
                self.PrintersWindow.show()

    def on_show_completed_jobs_activate(self, menuitem):
        if menuitem.get_active():
            self.which_jobs = "all"
        else:
            self.which_jobs = "not-completed"
        self.refresh()

    def on_show_printer_status_activate(self, menuitem):
        if self.show_printer_status.get_active ():
            self.PrintersWindow.show()
        else:
            self.PrintersWindow.hide()

    def check_still_connecting(self):
        """Timer callback to check on connecting-to-device reasons."""
        if self.update_connecting_devices ():
            self.get_notifications ()

        # Don't run this callback again.
        return False

    def update_connecting_devices(self):
        """Updates connecting_to_device dict and still_connecting set.
        Returns True if a device has been connecting too long."""
        time_now = time.time ()
        connecting_to_device = {}
        trouble = False
        for printer, reasons in self.printer_state_reasons.iteritems ():
            for reason in reasons:
                if reason.get_reason () == "connecting-to-device":
                    # Build a new connecting_to_device dict.  If our existing
                    # dict already has an entry for this printer, use that.
                    printer = reason.get_printer ()
                    t = self.connecting_to_device.get (printer, time_now)
                    connecting_to_device[printer] = t
                    if time_now - t >= CONNECTING_TIMEOUT:
                        trouble = True

        # Clear any previously-notified errors that are now fine.
        remove = set()
        for printer in self.still_connecting:
            if not self.connecting_to_device.has_key (printer):
                remove.add (printer)
                if self.trayicon and self.notify:
                    r = self.notified_reason
                    if (r.get_printer () == printer and
                        r.get_reason () == 'connecting-to-device'):
                        # We had sent a notification for this reason.
                        # Close it.
                        self.notify.close ()
                        self.notify = None

        self.still_connecting = self.still_connecting.difference (remove)

        self.connecting_to_device = connecting_to_device
        return trouble

    def check_state_reasons(self, my_printers=set(), printer_jobs={}):
        # Look for any new reasons since we last checked.
        old_reasons_seen_keys = self.reasons_seen.keys ()
        reasons_now = set()
        need_recheck = False
        for printer, reasons in self.printer_state_reasons.iteritems ():
            for reason in reasons:
                tuple = reason.get_tuple ()
                printer = reason.get_printer ()
                reasons_now.add (tuple)
                if not self.reasons_seen.has_key (tuple):
                    # New reason.
                    iter = self.store_printers.append (None)
                    self.store_printers.set_value (iter, 0,
                                                   reason.get_level ())
                    self.store_printers.set_value (iter, 1,
                                                   reason.get_printer ())
                    title, text = reason.get_description ()
                    self.store_printers.set_value (iter, 2, text)
                    self.reasons_seen[tuple] = iter
                    if (reason.get_reason () == "connecting-to-device" and
                        not self.connecting_to_device.has_key (printer)):
                        # First time we've seen this.
                        need_recheck = True

        if need_recheck:
            # Check on them again in a minute's time.
            gobject.timeout_add (CONNECTING_TIMEOUT * 1000,
                                 self.check_still_connecting)

        self.update_connecting_devices ()
        items = self.reasons_seen.keys ()
        for tuple in items:
            if not tuple in reasons_now:
                # Reason no longer present.
                iter = self.reasons_seen[tuple]
                self.store_printers.remove (iter)
                del self.reasons_seen[tuple]
                if (self.trayicon and self.notify and
                    self.notified_reason.get_tuple () == tuple):
                    # We had sent a notification for this reason.  Close it.
                    self.notify.close ()
                    self.notify = None

        # Update statusbar and icon with most severe printer reason
        # across all printers.
        self.icon_has_emblem = False
        reason = worst_printer_state_reason (self.printer_state_reasons)
        if reason != None and reason.get_level () >= StateReason.WARNING:
            title, text = reason.get_description ()
            if self.statusbar_set:
                self.statusbar.pop (0)
            self.statusbar.push (0, text)
            self.worst_reason_text = text
            self.statusbar_set = True

            if self.trayicon:
                icon = StateReason.LEVEL_ICON[reason.get_level ()]
                pixbuf = self.statusicon.get_pixbuf ().copy ()
                theme = gtk.icon_theme_get_default ()
                try:
                    emblem = theme.load_icon (icon, 22, 0)
                    emblem.composite (pixbuf,
                                      pixbuf.get_width () / 2,
                                      pixbuf.get_height () / 2,
                                      emblem.get_width () / 2,
                                      emblem.get_height () / 2,
                                      pixbuf.get_width () / 2,
                                      pixbuf.get_height () / 2,
                                      0.5, 0.5,
                                      gtk.gdk.INTERP_BILINEAR, 255)
                    self.set_statusicon_from_pixbuf (pixbuf)
                    self.icon_has_emblem = True
                except gobject.GError, exc:
                    pass # Couldn't load icon.
        else:
            # No errors
            if self.statusbar_set:
                self.statusbar.pop (0)
                self.statusbar_set = False

        # Send notifications for printers we've got jobs queued for.
        my_reasons = {}
        for printer in my_printers:
            if self.printer_state_reasons.has_key (printer):
                my_reasons[printer] = self.printer_state_reasons[printer]
        reason = worst_printer_state_reason (my_reasons)

        # If connecting-to-device is the worst reason, check if it's been
        # like that for more than a minute.  If so, and there is job being
        # processed for that device, let's put a warning bubble up.
        if (self.trayicon and reason != None and
            reason.get_reason () == "connecting-to-device"):
            now = time.time ()
            printer = reason.get_printer ()
            start = self.connecting_to_device.get (printer, now)
            if now - start >= CONNECTING_TIMEOUT:
                have_processing_job = False
                for job, data in printer_jobs.get (printer, {}).iteritems ():
                    state = data.get ('job-state', cups.IPP_JOB_CANCELED)
                    if state == cups.IPP_JOB_PROCESSING:
                        have_processing_job = True
                        break

                if have_processing_job:
                    # This will be in our list of reasons we've already seen,
                    # which ordinarily stops us notifying the user.  In this
                    # case, pretend we haven't seen it before.
                    self.still_connecting.add (printer)
                    old_reasons_seen_keys.remove (reason.get_tuple ())
                    reason = StateReason (printer,
                                          reason.get_reason () + "-error")

        if (self.trayicon and reason != None and
            reason.get_level () >= StateReason.WARNING):
            if not reason.get_tuple () in old_reasons_seen_keys:
                level = reason.get_level ()
                if level == StateReason.WARNING:
                    notify_urgency = pynotify.URGENCY_LOW
                    timeout = pynotify.EXPIRES_DEFAULT
                else:
                    notify_urgency = pynotify.URGENCY_NORMAL
                    timeout = pynotify.EXPIRES_NEVER

                (title, text) = reason.get_description ()

                if self.notify:
                    self.notify.close ()
                self.notify = pynotify.Notification (title, text, 'printer')
                self.set_statusicon_visibility ()
                # Let the icon show itself, ready for the notification
                while gtk.events_pending ():
                    gtk.main_iteration ()

                self.notify.attach_to_status_icon (self.statusicon)

                while gtk.events_pending ():
                    gtk.main_iteration ()

                self.notify.set_urgency (notify_urgency)
                self.notify.set_timeout (timeout)
                self.notify.connect ('closed', self.on_notification_closed)
                self.notify.show ()
                self.notified_reason = reason

    def on_notification_closed(self, notify):
        self.notify = None
        reason = self.notified_reason
        if reason.get_reason () == "connecting-to-device":
            try:
                del self.connecting_to_device[reason.get_printer ()]
            except KeyError:
                pass

        if self.trayicon:
            # Any reason to keep the status icon around?
            self.set_statusicon_visibility ()

    def update_job_creation_times(self):
        now = time.time ()
        need_update = False
        for job, data in self.jobs.iteritems():
            if self.jobs.has_key (job):
                iter = self.jobiters[job]

            t = "Unknown"
            if data.has_key ('time-at-creation'):
                created = data['time-at-creation']
                ago = now - created
                if ago > 86400:
                    t = time.ctime (created)
                elif ago > 3600:
                    need_update = True
                    hours = int (ago / 3600)
                    mins = int ((ago % 3600) / 60)
                    if hours == 1:
                        if mins == 0:
                            t = _("1 hour ago")
                        elif mins == 1:
                            t = _("1 hour and 1 minute ago")
                        else:
                            t = _("1 hour and %d minutes ago") % mins
                    else:
                        if mins == 0:
                            t = _("%d hours ago") % hours
                        elif mins == 1:
                            t = _("%d hours and 1 minute ago") % hours
                        else:
                            t = _("%d hours and %d minutes ago") % \
                                (hours, mins)
                else:
                    need_update = True
                    mins = ago / 60
                    if mins < 2:
                        t = _("a minute ago")
                    else:
                        t = _("%d minutes ago") % mins

            self.store.set_value (iter, 4, t)

        if need_update and not self.will_update_job_creation_times:
            gobject.timeout_add (60 * 1000,
                                 self.update_job_creation_times)
            self.will_update_job_creation_times = True

        if not need_update:
            self.will_update_job_creation_times = False

        # Return code controls whether the timeout will recur.
        return self.will_update_job_creation_times

    def print_error_dialog_response(self, dialog, response):
        dialog.hide ()
        dialog.destroy ()
        if response == gtk.RESPONSE_NO:
            # Diagnose
            if not self.__dict__.has_key ('troubleshooter'):
                import troubleshoot
                troubleshooter = troubleshoot.run (self.on_troubleshoot_quit)
                self.troubleshooter = troubleshooter

    def on_troubleshoot_quit(self, troubleshooter):
        del self.troubleshooter

    def get_notifications(self):
        debugprint ("get_notifications")
        try:
            c = cups.Connection ()

            try:
                try:
                    notifications = c.getNotifications ([self.sub_id],
                                                        [self.sub_seq + 1])
                except AttributeError:
                    notifications = c.getNotifications ([self.sub_id])
            except cups.IPPError, (e, m):
                if e == cups.IPP_NOT_FOUND:
                    # Subscription lease has expired.
                    self.sub_id = -1
                    self.refresh ()
                    return False

                return True
        except:
            return True

        jobs = self.jobs.copy ()
        for event in notifications['events']:
            seq = event['notify-sequence-number']
            try:
                if seq <= self.sub_seq:
                    # Work around a bug in pycups < 1.9.34
                    continue
            except AttributeError:
                pass
            self.sub_seq = seq
            nse = event['notify-subscribed-event']
            debugprint ("%d %s %s" % (seq, nse, event['notify-text']))
            debugprint (pprint.pformat (event))
            if nse.startswith ('printer-'):
                # Printer events
                name = event['printer-name']
                if nse == 'printer-deleted':
                    if self.printer_state_reasons.has_key (name):
                        del self.printer_state_reasons[name]
                else:
                    printer_state_reasons = event['printer-state-reasons']
                    if type (printer_state_reasons) != list:
                        # Work around a bug in pycups < 1.9.36
                        printer_state_reasons = [printer_state_reasons]

                    reasons = []
                    for reason in printer_state_reasons:
                        if reason == "none":
                            break
                        if state_reason_is_harmless (reason):
                            continue
                        reasons.append (StateReason (name, reason))
                    self.printer_state_reasons[name] = reasons
                continue

            # Job events
            jobid = event['notify-job-id']
            if nse == 'job-created':
                if (self.specific_dests != None and
                    event['printer-name'] not in self.specific_dests):
                    continue

                try:
                    attrs = c.getJobAttributes (jobid)
                    if (self.my_jobs and
                        attrs['job-originating-user-name'] != cups.getUser ()):
                        continue

                    jobs[jobid] = {'job-k-octets': attrs['job-k-octets']}
                except AttributeError:
                    jobs[jobid] = {'job-k-octets': 0}
            elif nse == 'job-completed':
                if self.which_jobs == "not-completed":
                    try:
                        del jobs[jobid]
                    except KeyError:
                        pass
                    continue

            try:
                job = jobs[jobid]
            except KeyError:
                continue

            for attribute in ['job-state',
                              'job-name']:
                job[attribute] = event[attribute]
            if event.has_key ('notify-printer-uri'):
                job['job-printer-uri'] = event['notify-printer-uri']

            if nse == 'job-stopped' and self.trayicon:
                # Why has the job stopped?  It might be due to a job
                # error of some sort, or it might be that the backend
                # requires authentication.  If the latter, the job will
                # be held not stopped, and the job-hold-until attribute
                # will be 'auth-info-required'.
                if job['job-state'] == cups.IPP_JOB_HELD:
                    try:
                        # Fetch the job-hold-until attribute, as this is
                        # not provided in the notification attributes.
                        job = c.getJobAttributes (jobid)
                        jobs[jobid] = job
                    except cups.IPPError:
                        pass

                if (job['job-state'] == cups.IPP_JOB_HELD and
                    job['job-hold-until'] == 'auth-info-required'):
                    # TODO: ask user for authentication and authenticate
                    # the job.
                    debugprint ("Authentication required")
                    continue

                # Other than that, unfortunately the only
                # clue we get is the notify-text, which is not
                # translated into our native language.  We'd better
                # try parsing it.  In CUPS-1.3.6 the possible strings
                # are:
                #
                # "Job stopped due to filter errors; please consult
                # the error_log file for details."
                #
                # "Job stopped due to backend errors; please consult
                # the error_log file for details."
                #
                # "Job held due to backend errors; please consult the
                # error_log file for details."
                #
                # "Authentication is required for job %d."
                notify_text = event['notify-text']
                document = job['job-name']
                if notify_text.find ("backend errors") != -1:
                    message = _("There was a problem sending document `%s' "
                                "(job %d) to the printer.") % (document, jobid)
                elif notify_text.find ("filter errors") != -1:
                    message = _("There was a problem processing document `%s' "
                                "(job %d).") % (document, jobid)
                else:
                    # Give up and use the untranslated provided.
                    message = _("There was a problem printing document `%s' "
                                "(job %d): `%s'.") % (document, jobid,
                                                      notify_text)

                self.toggle_window_display (self.statusicon, force_show=True)
                dialog = gtk.Dialog (_("Print Error"), self.MainWindow, 0,
                                     (_("_Diagnose"), gtk.RESPONSE_NO,
                                        gtk.STOCK_OK, gtk.RESPONSE_OK))
                dialog.set_default_response (gtk.RESPONSE_OK)
                dialog.set_border_width (6)
                dialog.set_resizable (False)
                dialog.set_icon_name (ICON)
                hbox = gtk.HBox (False, 12)
                hbox.set_border_width (6)
                image = gtk.Image ()
                image.set_from_stock ('gtk-dialog-error',
                                      gtk.ICON_SIZE_DIALOG)
                hbox.pack_start (image, False, False, 0)
                vbox = gtk.VBox (False, 12)

                markup = ('<span weight="bold" size="larger">' +
                          _("Print Error") + '</span>\n\n' +
                          message)
                try:
                    if event['printer-state'] == cups.IPP_PRINTER_STOPPED:
                        name = event['printer-name']
                        markup += ' '
                        markup += (_("The printer called `%s' has "
                                     "been disabled.") % name)
                except KeyError:
                    pass

                label = gtk.Label (markup)
                label.set_use_markup (True)
                label.set_line_wrap (True)
                label.set_alignment (0, 0)
                vbox.pack_start (label, False, False, 0)
                hbox.pack_start (vbox, False, False, 0)
                dialog.vbox.pack_start (hbox)
                dialog.connect ('response', self.print_error_dialog_response)
                dialog.show_all ()

        self.update (jobs)
        self.jobs = jobs
        return False

    def refresh(self):
        debugprint ("refresh")

        try:
            c = cups.Connection ()
        except RuntimeError:
            return

        if self.sub_id != -1:
            c.cancelSubscription (self.sub_id)
            gobject.source_remove (self.update_timer)
            debugprint ("Canceled subscription %d" % self.sub_id)

        try:
            del self.sub_seq
        except AttributeError:
            pass
        self.sub_id = c.createSubscription ("/",
                                            events=["job-created",
                                                    "job-completed",
                                                    "job-stopped",
                                                    "job-progress",
                                                    "job-state-changed",
                                                    "printer-deleted",
                                                    "printer-state-changed"])
        self.update_timer = gobject.timeout_add (MIN_REFRESH_INTERVAL * 1000,
                                                 self.get_notifications)
        debugprint ("Created subscription %d" % self.sub_id)

        try:
            jobs = c.getJobs (which_jobs=self.which_jobs, my_jobs=self.my_jobs)
            self.printer_state_reasons = collect_printer_state_reasons (c)
        except cups.IPPError, (e, m):
            self.show_IPP_Error (e, m)
            return
        except RuntimeError:
            return

        if self.specific_dests != None:
            for jobid in jobs.keys ():
                uri = jobs[jobid].get('job-printer-uri', '/')
                i = uri.rfind ('/')
                printer = uri[i + 1:]
                if printer not in self.specific_dests:
                    del jobs[jobid]

        self.update (jobs)

        self.jobs = jobs
        self.update_job_creation_times ()
        return False

    def update(self, jobs):
        debugprint ("update")
        # Count active jobs
        if self.which_jobs == "not-completed":
            active_jobs = jobs
        else:
            active_jobs = filter (lambda x:
                                      x['job-state'] <= cups.IPP_JOB_STOPPED,
                                  jobs.values ())
        num_jobs = len (active_jobs)

        if self.trayicon:
            self.num_jobs = num_jobs
            if self.hidden and self.num_jobs != self.num_jobs_when_hidden:
                self.hidden = False
            if num_jobs == 0:
                tooltip = _("No documents queued")
                self.set_statusicon_from_pixbuf (self.icon_no_jobs)
            elif num_jobs == 1:
                tooltip = _("1 document queued")
                self.set_statusicon_from_pixbuf (self.icon_jobs)
            else:
                tooltip = _("%d documents queued") % num_jobs
                self.set_statusicon_from_pixbuf (self.icon_jobs)

        my_printers = set()
        printer_jobs = {}
        for job, data in jobs.iteritems ():
            state = data.get ('job-state', cups.IPP_JOB_CANCELED)
            if state >= cups.IPP_JOB_CANCELED:
                continue
            uri = data.get ('job-printer-uri', '/')
            i = uri.rfind ('/')
            printer = uri[i + 1:]
            my_printers.add (printer)
            if not printer_jobs.has_key (printer):
                printer_jobs[printer] = {}
            printer_jobs[printer][job] = data

        self.check_state_reasons (my_printers, printer_jobs)

        if self.trayicon:
            # If there are no jobs but there is a printer
            # warning/error indicated by the icon, set the icon
            # tooltip to the reason description.
            if self.num_jobs == 0 and self.icon_has_emblem:
                tooltip = self.worst_reason_text

            self.statusicon.set_tooltip (tooltip)
            self.set_statusicon_visibility ()

        for job in self.jobs:
            if not jobs.has_key (job):
                self.store.remove (self.jobiters[job])
                del self.jobiters[job]

        for job, data in jobs.iteritems():
            if self.jobs.has_key (job):
                iter = self.jobiters[job]
            else:
                iter = self.store.append (None)
                self.store.set_value (iter, 0, job)
                self.store.set_value (iter, 1, data.get('job-name', 'Unknown'))
                self.jobiters[job] = iter

            printer = "Unknown"
            uri = data.get('job-printer-uri', '')
            i = uri.rfind ('/')
            if i != -1:
                printer = uri[i + 1:]
            self.store.set_value (iter, 2, printer)

            if data.has_key ('job-k-octets'):
                size = str (data['job-k-octets']) + 'k'
            else:
                size = 'Unknown'
            self.store.set_value (iter, 3, size)

            state = None
            if data.has_key ('job-state'):
                try:
                    jstate = data['job-state']
                    s = int (jstate)
                    state = { cups.IPP_JOB_PENDING: _("Pending"),
                              cups.IPP_JOB_HELD: _("Held"),
                              cups.IPP_JOB_PROCESSING: _("Processing"),
                              cups.IPP_JOB_STOPPED: _("Stopped"),
                              cups.IPP_JOB_CANCELED: _("Canceled"),
                              cups.IPP_JOB_ABORTED: _("Aborted"),
                              cups.IPP_JOB_COMPLETED: _("Completed") }[s]
                except ValueError:
                    pass
                except IndexError:
                    pass    
            if state == None:
                state = _("Unknown")
            self.store.set_value (iter, 5, state)

    def set_statusicon_visibility (self):
        if self.trayicon:
            if self.suppress_icon_hide:
                # Avoid hiding the icon if we've been woken up to notify
                # about a new printer.
                self.suppress_icon_hide = False
                return

            self.statusicon.set_visible ((not self.hidden) and
                                         (self.num_jobs > 0 or
                                          self.icon_has_emblem or
                                          (self.notify != None)) or
                                          self.special_status_icon)

    def on_treeview_button_press_event(self, treeview, event):
        if event.button != 3:
            return

        # Right-clicked.
        store, iter = treeview.get_selection ().get_selected ()
        if iter == None:
            return

        self.jobid = self.store.get_value (iter, 0)
        job = self.jobs[self.jobid]
        self.cancel.set_sensitive (True)
        self.hold.set_sensitive (True)
        self.release.set_sensitive (True)
        self.reprint.set_sensitive (True)
        if job.has_key ('job-state'):
            s = job['job-state']
            if s >= cups.IPP_JOB_CANCELED:
                self.cancel.set_sensitive (False)
            if s != cups.IPP_JOB_PENDING:
                self.hold.set_sensitive (False)
            if s != cups.IPP_JOB_HELD:
                self.release.set_sensitive (False)
            if (not job.get('job-preserved', False)):
                self.reprint.set_sensitive (False)
        self.job_popupmenu.popup (None, None, None, event.button,
                                  event.get_time ())

    def on_icon_popupmenu(self, icon, button, time):
        self.icon_popupmenu.popup (None, None, None, button, time)

    def on_icon_hide_activate(self, menuitem):
        if self.notify:
            self.notify.close ()
            self.notify = None

        self.num_jobs_when_hidden = self.num_jobs
        self.hidden = True
        self.set_statusicon_visibility ()

    def on_icon_quit_activate (self, menuitem):
        if self.loop:
            self.loop.quit ()

    def on_job_cancel_activate(self, menuitem):
        try:
            c = authconn.Connection (self.MainWindow)
            c.cancelJob (self.jobid)
            del c
        except cups.IPPError, (e, m):
            self.show_IPP_Error (e, m)
            return
        except RuntimeError:
            return

    def on_job_hold_activate(self, menuitem):
        try:
            c = authconn.Connection (self.MainWindow)
            c.setJobHoldUntil (self.jobid, "indefinite")
            del c
        except cups.IPPError, (e, m):
            self.show_IPP_Error (e, m)
            return
        except RuntimeError:
            return

    def on_job_release_activate(self, menuitem):
        try:
            c = authconn.Connection (self.MainWindow)
            c.setJobHoldUntil (self.jobid, "no-hold")
            del c
        except cups.IPPError, (e, m):
            self.show_IPP_Error (e, m)
            return
        except RuntimeError:
            return

    def on_job_reprint_activate(self, menuitem):
        try:
            c = authconn.Connection (self.MainWindow)
            c.restartJob (self.jobid)
            del c
        except cups.IPPError, (e, m):
            self.show_IPP_Error (e, m)
            return
        except RuntimeError:
            return

    def on_refresh_activate(self, menuitem):
        self.refresh ()

    def handle_dbus_signal(self, *args):
        gobject.source_remove (self.update_timer)
        self.update_timer = gobject.timeout_add (200, self.get_notifications)

    ## Printer status window
    def set_printer_status_icon (self, column, cell, model, iter, *user_data):
        level = model.get_value (iter, 0)
        icon = StateReason.LEVEL_ICON[level]
        theme = gtk.icon_theme_get_default ()
        try:
            pixbuf = theme.load_icon (icon, 22, 0)
            cell.set_property("pixbuf", pixbuf)
        except gobject.GError, exc:
            pass # Couldn't load icon

    def set_printer_status_name (self, column, cell, model, iter, *user_data):
        cell.set_property("text", model.get_value (iter, 1))
