Name:           waylookup
Version:        0.2.0
Release:        1%{?dist}
Summary:        Click-to-translate screen overlay for Wayland
License:        MIT
URL:            https://github.com/DarolCup/WayLookup
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros

Requires:       python3-gobject
Requires:       python3-cairo
Requires:       python3-pillow
Requires:       gtk3
Requires:       tesseract
Requires:       tesseract-langpack-eng
Recommends:     tesseract-langpack-rus
Recommends:     (spectacle if plasma-workspace)
Recommends:     (libkscreen if plasma-workspace)
Recommends:     (gtk-layer-shell if plasma-workspace)
Recommends:     (libappindicator-gtk3 if plasma-workspace)
Recommends:     (xdg-desktop-portal-gnome if gnome-shell)
Recommends:     (pipewire-gstreamer if gnome-shell)
Recommends:     (gstreamer1-plugins-good if gnome-shell)

%description
WayLookup captures the screen, recognises text with Tesseract and shows
dictionary meanings and a sentence translation for the word you click.
Works on Wayland with KDE Plasma, GNOME and Hyprland.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l waylookup
install -Dm644 packaging/io.github.waylookup.WayLookup.desktop \
  %{buildroot}%{_datadir}/applications/io.github.waylookup.WayLookup.desktop
install -Dm644 packaging/waylookup.service %{buildroot}%{_userunitdir}/waylookup.service
install -Dm644 examples/en-ru.json %{buildroot}%{_datadir}/waylookup/examples/en-ru.json

%check
%{python3} -m unittest discover -s tests -v

%files -f %{pyproject_files}
%doc README.md CHANGELOG.md
%{_bindir}/waylookup
%{_datadir}/applications/io.github.waylookup.WayLookup.desktop
%{_userunitdir}/waylookup.service
%{_datadir}/waylookup/

%changelog
* Sun Nov 08 2026 WayLookup contributors - 0.2.0-1
- Release 0.2.0
