"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import gtk modules
import gi

from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.backend.DeckManagement.HelperMethods import add_default_keys

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango

# Import Python modules
from loguru import logger as log

# Import globals
import globals as gl

# Import own modules
from src.windows.mainWindow.elements.Sidebar.elements.IconSelector import IconSelector
from src.backend.PageManagement.Page import Page
from src.backend.DeckManagement.DeckController import DeckController, KeyLabel
from GtkHelper.GtkHelper import RevertButton


class ImageEditor(Gtk.Box):
    def __init__(self, sidebar, media_key="media", title=None, **kwargs):
        self.sidebar = sidebar
        self.media_key = media_key
        self.title = title
        super().__init__(**kwargs)
        self.build()

    def build(self):
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.image_group = ImageGroup(self.sidebar, self.media_key, self.title)
        self.main_box.append(self.image_group)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.image_group.load_for_identifier(identifier, state)


class ImageGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar, media_key="media", title=None, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.media_key = media_key
        self.title = title

        self.build()

    def build(self):
        # Swap button first, always visible - clicking it copies the image
        # source and every layout style to the other slot.
        self.swap_row = SwapLayoutRow(sidebar=self.sidebar, media_key=self.media_key)
        self.add(self.swap_row)

        self.expander = Layout(self, self.media_key, self.title)
        self.add(self.expander)

        return

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.swap_row.load_for_identifier(identifier, state)
        self.expander.load_for_identifier(identifier, state)


class Layout(Adw.ExpanderRow):
    def __init__(self, margin_group, media_key, title):
        super().__init__(title=title or gl.lm.get("right-area.image-editor.layout.header"), subtitle=gl.lm.get("right-area.image-editor.layout.subtitle"))
        self.margin_group = margin_group
        self.media_key = media_key
        self.identifier: InputIdentifier = None
        self.active_state: int = None
        self.build()

    def build(self):
        self.size_row = SizeRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.size_row)

        self.valign_row = ValignRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.valign_row)

        self.halign_row = HalignRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.halign_row)

        self.opacity_row = OpacityRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.opacity_row)

        self.speed_row = SpeedRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.speed_row)

        self.blend_mode_row = BlendModeRow(sidebar=self.margin_group.sidebar, media_key=self.media_key)
        self.add_row(self.blend_mode_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.active_identifier = identifier
        self.active_state = state

        self.size_row.load_for_identifier(identifier, state)
        self.valign_row.load_for_identifier(identifier, state)
        self.halign_row.load_for_identifier(identifier, state)
        self.opacity_row.load_for_identifier(identifier, state)
        self.speed_row.load_for_identifier(identifier, state)
        self.blend_mode_row.load_for_identifier(identifier, state)


class SwapLayoutRow(Adw.ActionRow):
    """A prominent button that swaps the key's main image with its second
    image - the image source AND every layout style (size, valign, halign,
    opacity, speed, blend mode) move to the other slot and back."""

    def __init__(self, sidebar, media_key="media", **kwargs):
        self.sidebar = sidebar
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        title = (
            gl.lm.get("right-area.image-editor.layout.swap-with-second", "Swap with second image")
            if media_key == "media"
            else gl.lm.get("right-area.image-editor.layout.swap-with-first", "Swap with first image")
        )
        subtitle = (
            gl.lm.get("right-area.image-editor.layout.swap-with-second.subtitle",
                      "Copy image + layout to the second slot")
            if media_key == "media"
            else gl.lm.get("right-area.image-editor.layout.swap-with-first.subtitle",
                           "Copy image + layout back to the first slot")
        )
        super().__init__(title=title, subtitle=subtitle, **kwargs)

        # A real, visible button - not just an activatable row.
        self.swap_button = Gtk.Button(label="⇄", tooltip_text=title)
        self.swap_button.add_css_class("suggested-action")
        self.swap_button.set_valign(Gtk.Align.CENTER)
        self.add_suffix(self.swap_button)
        self.set_activatable_widget(self.swap_button)
        self.swap_button.connect("clicked", self.on_swap)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.active_identifier = identifier
        self.active_state = state
        # Only keys have a second image slot.
        self.set_visible(isinstance(identifier, Input.Key))

    def on_swap(self, *args):
        try:
            self._perform_swap()
        except Exception as e:
            log.error(f"Failed to swap media layouts: {e}")

    def _perform_swap(self):
        identifier = self.active_identifier
        state = self.active_state
        if identifier is None or state is None:
            return
        controller = gl.app.main_win.get_active_controller()
        if controller is None or controller.active_page is None:
            return
        page = controller.active_page
        d = identifier.get_config(page)
        st = (d.get("states") or {}).get(str(state))
        if not isinstance(st, dict):
            return

        media = st.get("media")
        media2 = st.get("media-2")
        layout = st.get("layout")
        layout2 = st.get("media-2-layout")

        # Swap main <-> second (content and layout). A missing side swaps
        # with the empty slot, so single-image keys just move the image over.
        st["media"], st["media-2"] = media2, media
        st["layout"], st["media-2-layout"] = layout2, layout
        for key in ("media", "media-2", "layout", "media-2-layout"):
            if st.get(key) is None:
                st.pop(key, None)

        page.save()
        gl.page_manager.update_dict_of_pages_with_path(page.json_path)
        controller.load_input(controller.get_input(identifier), page, update=True)

        # Refresh the sidebar so both layout forms (and the icon selectors)
        # show the swapped values.
        try:
            self.sidebar.key_editor.load_for_identifier(identifier, state)
        except Exception:
            pass


class SizeRow(Adw.PreferencesRow):
    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.build()

        self.connect_signals()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("right-area.image-editor.layout.size.label"), hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.size_spinner = SpinButton(0, 200, 1)
        self.main_box.append(self.size_spinner)

        self.size_spinner.revert_button.connect("clicked", self.on_size_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return

        controller_input = controller.get_input(identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        use_page_properties = layout_manager.get_use_page_layout_properties()
        self.size_spinner.revert_button.set_sensitive(use_page_properties.get("size", False))

        self.update_values()

        self.connect_signals()

    def update_values(self, composed_label: KeyLabel = None):
        self.disconnect_signals()
        if composed_label is None:
            visible_child = gl.app.main_win.leftArea.deck_stack.get_visible_child()
            if visible_child is None:
                return
            controller = visible_child.deck_controller
            if controller is None:
                return
            controller_input = controller.get_input(self.active_identifier)
            layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
            composed_label = layout_manager.get_composed_layout()

        self.size_spinner.button.set_value(composed_label.size*100)

        self.connect_signals()

    def on_size_changed(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "size", widget.get_value()/100)

        self.size_spinner.revert_button.set_sensitive(True)

    def on_size_reset(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "size", None)

        self.size_spinner.revert_button.set_sensitive(False)
        self.update_values()

    def connect_signals(self):
        self.size_spinner.button.connect("value-changed", self.on_size_changed)

    def disconnect_signals(self):
        try:
            self.size_spinner.button.disconnect_by_func(self.on_size_changed)
        except:
            pass


class AlignmentRow(Adw.PreferencesRow):
    def __init__(self, sidebar, label_text, property_name, media_key="media", **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.property_name = property_name
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        self.build(label_text)

        self.connect_signals()

    def build(self, label_text):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=label_text, hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.alignment_spinner = SpinButton(-1, 1, 0.1)
        self.main_box.append(self.alignment_spinner)

        self.alignment_spinner.revert_button.connect("clicked", self.on_alignment_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.active_identifier = identifier
        self.active_state = state
        self.disconnect_signals()

        controller = gl.app.main_win.get_active_controller()

        controller_input = controller.get_input(identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        use_page_properties = layout_manager.get_use_page_layout_properties()
        self.alignment_spinner.revert_button.set_sensitive(use_page_properties.get(self.property_name, False))

        self.connect_signals()
        self.update_values()

    def update_values(self, composed_label: KeyLabel = None):
        self.disconnect_signals()
        if composed_label is None:
            controller = gl.app.main_win.get_active_controller()
            if controller is None:
                return
            controller_input = controller.get_input(self.active_identifier)
            layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
            composed_label = layout_manager.get_composed_layout()

        self.alignment_spinner.button.set_value(getattr(composed_label, self.property_name))

        self.connect_signals()

    def on_alignment_changed(self, widget):
        active_page = gl.app.main_win.get_active_page()

        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, self.property_name, widget.get_value())

        self.alignment_spinner.revert_button.set_sensitive(True)

    def on_alignment_reset(self, widget):
        active_page = gl.app.main_win.get_active_page()

        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, self.property_name, None)

        self.alignment_spinner.revert_button.set_sensitive(False)
        self.update_values()

    def connect_signals(self):
        self.alignment_spinner.button.connect("value-changed", self.on_alignment_changed)

    def disconnect_signals(self):
        self.alignment_spinner.button.disconnect_by_func(self.on_alignment_changed)

class ValignRow(AlignmentRow):
    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(sidebar, label_text=gl.lm.get("right-area.image-editor.layout.valign.label"), property_name="valign", media_key=media_key, **kwargs)

class HalignRow(AlignmentRow):
    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(sidebar, label_text=gl.lm.get("right-area.image-editor.layout.halign.label"), property_name="halign", media_key=media_key, **kwargs)


class OpacityRow(Adw.PreferencesRow):
    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        self.build()
        self.connect_signals()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Opacity", hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.opacity_spinner = SpinButton(0, 100, 1)
        self.main_box.append(self.opacity_spinner)

        self.opacity_spinner.revert_button.connect("clicked", self.on_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return

        controller_input = controller.get_input(identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        use_page = layout_manager.get_use_page_layout_properties()
        self.opacity_spinner.revert_button.set_sensitive(use_page.get("opacity", False))

        self.update_values()
        self.connect_signals()

    def update_values(self):
        self.disconnect_signals()
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        controller_input = controller.get_input(self.active_identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        layout = layout_manager.get_composed_layout()
        self.opacity_spinner.button.set_value(int(layout.opacity * 100))
        self.connect_signals()

    def on_changed(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "opacity", widget.get_value() / 100)
        self.opacity_spinner.revert_button.set_sensitive(True)

    def on_reset(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "opacity", None)
        self.opacity_spinner.revert_button.set_sensitive(False)
        self.update_values()

    def connect_signals(self):
        self.opacity_spinner.button.connect("value-changed", self.on_changed)

    def disconnect_signals(self):
        try:
            self.opacity_spinner.button.disconnect_by_func(self.on_changed)
        except:
            pass


class SpeedRow(Adw.PreferencesRow):
    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        self.build()
        self.connect_signals()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="GIF Speed", hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.speed_spinner = SpinButton(10, 300, 1)
        self.main_box.append(self.speed_spinner)

        self.speed_spinner.revert_button.connect("clicked", self.on_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return

        controller_input = controller.get_input(identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        use_page = layout_manager.get_use_page_layout_properties()
        self.speed_spinner.revert_button.set_sensitive(use_page.get("speed", False))

        self.update_values()
        self.connect_signals()

    def update_values(self):
        self.disconnect_signals()
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        controller_input = controller.get_input(self.active_identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        layout = layout_manager.get_composed_layout()
        self.speed_spinner.button.set_value(int(layout.speed * 100))
        self.connect_signals()

    def on_changed(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "speed", widget.get_value() / 100)
        self.speed_spinner.revert_button.set_sensitive(True)

    def on_reset(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "speed", None)
        self.speed_spinner.revert_button.set_sensitive(False)
        self.update_values()

    def connect_signals(self):
        self.speed_spinner.button.connect("value-changed", self.on_changed)

    def disconnect_signals(self):
        try:
            self.speed_spinner.button.disconnect_by_func(self.on_changed)
        except:
            pass


class BlendModeRow(Adw.PreferencesRow):
    BLEND_MODES = (
        "normal", "multiply", "screen", "darken", "lighten", "hard-light",
        "overlay", "color-dodge", "color-burn", "difference", "exclusion",
        "soft-light",
    )

    def __init__(self, sidebar, media_key="media", **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.media_key = media_key
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        self.build()
        self.connect_signals()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Blend Mode", hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.combo = Gtk.DropDown.new_from_strings(self.BLEND_MODES)
        self.main_box.append(self.combo)

        self.revert_button = RevertButton()
        self.main_box.append(self.revert_button)

        self.revert_button.connect("clicked", self.on_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return

        controller_input = controller.get_input(identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        use_page = layout_manager.get_use_page_layout_properties()
        self.revert_button.set_sensitive(use_page.get("blend-mode", False))

        self.update_values()
        self.connect_signals()

    def update_values(self):
        self.disconnect_signals()
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        controller_input = controller.get_input(self.active_identifier)
        layout_manager = controller_input.get_active_state().layout_manager if self.media_key == "media" else controller_input.get_active_state().media_2_layout_manager
        layout = layout_manager.get_composed_layout()
        mode = layout.blend_mode if layout.blend_mode in self.BLEND_MODES else "normal"
        self.combo.set_selected(self.BLEND_MODES.index(mode))
        self.connect_signals()

    def on_changed(self, widget, *args):
        index = self.combo.get_selected()
        if index < 0 or index >= len(self.BLEND_MODES):
            return
        mode = self.BLEND_MODES[index]
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "blend-mode", mode)
        self.revert_button.set_sensitive(True)

    def on_reset(self, widget):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_media_value(self.active_identifier, self.active_state, self.media_key, "blend-mode", None)
        self.revert_button.set_sensitive(False)
        self.update_values()

    def connect_signals(self):
        self.combo.connect("notify::selected", self.on_changed)

    def disconnect_signals(self):
        try:
            self.combo.disconnect_by_func(self.on_changed)
        except:
            pass


class SpinButton(Gtk.Box):
    def __init__(self, start: float, end: float, step: float, **kwargs):
        super().__init__(css_classes=["linked"], **kwargs)

        self.button = Gtk.SpinButton.new_with_range(start, end, step)
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)
