//! Per-panel document tabs with VS Code preview/pin semantics (#2282, ms-65 §2).
//!
//! # What this is
//!
//! Today both detail panes are a pure function of the sidebar selection: move
//! the selection and the issue you were reading is gone. [`DocTabs`] adds a
//! second, *independent* notion of "what am I looking at" — an ordered set of
//! open documents per panel, one of them active, and **at most one preview
//! slot** — so two issues can be held open at once and returning to one is a
//! click rather than a re-navigation.
//!
//! # The model
//!
//! A document key is `(repo, issue_number)`. Each [`PanelScope`] owns its own
//! [`DocTabGroup`]: an ordered `Vec<DocKey>`, the index of the active tab, and
//! optionally the index of the single preview tab. The scope split is what
//! keeps Board's and Pipeline's sets from merging (contract §3b) — #2282
//! wired `Board`; #2284 wires `Pipeline` against this same shape, one
//! `DocTabGroup` per scope, so the two never merge, reorder or drop into
//! each other when the active panel switches.
//!
//! # Open semantics (contract §2e)
//!
//! 1. Open a document that is already open → activate its tab. No new tab, no
//!    replace ([`DocTabGroup::open_preview`]'s first branch).
//! 2. Open a not-yet-open document while a preview tab exists → **replace the
//!    preview in place**: same index, same active state.
//! 3. Otherwise → append a new preview tab and activate it.
//! 4. Pinning ([`DocTabGroup::pin`]) is open-or-activate *followed by*
//!    promotion — it drops `is_preview`, which is what makes the tab stop being
//!    the replaceable slot.
//!
//! Invariant, checked by `debug_assert` on every mutation: there is never more
//! than one preview tab, and `preview`/`active` always index a live tab.
//!
//! # Why not quadraui's `TabGroupController`
//!
//! Its content is a `Box<dyn BackendWidget>`, which cannot borrow `&CoordApp`
//! — the detail pane's content is derived from app state on every frame, so
//! there is nothing to box. quadraui #596/#597 (`WorkspaceController` + the
//! preview tier) are the intended long-term home for this model and do not
//! exist at any pushed quadraui rev yet; when they land, this module is the
//! thing to delete in favour of them. The public surface here is deliberately
//! small and free of rendering concerns so that swap stays cheap.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use quadraui::primitives::split_tree::SplitDirection;
use quadraui::{PaletteItem, SplitTree, StyledSpan, StyledText};
use serde::{Deserialize, Serialize};

use crate::app::drive_queue::repo_alias;
use crate::app::format::{fuzzy_score, trunc};
use crate::app::types::{BoardData, BoardDetailTab, PipelineDetailTab};

/// Which panel's document set a tab belongs to.
///
/// Board's and Pipeline's tab sets, active tabs and preview slots are stored
/// per scope and never merge (contract §3b).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum PanelScope {
    Board,
    /// #2284: the Pipeline panel's own independent tab set — same
    /// preview/pin semantics as Board, stored and revealed against the
    /// Pipeline sidebar (never the Board's). See `pipeline.rs`'s
    /// "#2284 (ms-65 §3): Pipeline document tabs" section.
    Pipeline,
}

/// A document key: which repo, which issue number.
pub(crate) type DocKey = (String, u64);

/// Maximum rendered width, in display columns, of a document tab's
/// `#<N> <title>` label — inclusive of the `#<N> ` prefix (contract §2b).
///
/// Contract note: this is a pinned constant of the milestone, not a value
/// derived from anything in the codebase. It is chosen to keep 3–4 tabs
/// visible in the 82-column main panel without truncating short titles.
pub(crate) const DOC_TAB_LABEL_COLS: usize = 20;

/// #2288 (contract §9): the same budget for a tab in a **split** pane — a
/// second, independently-pinned constant, *not* [`DOC_TAB_LABEL_COLS`]
/// halved (which would be 10). At the default 50/50 split of the 82-column
/// main panel each pane gets roughly 40 columns, and 14 (16 with the §1
/// preview marker) is what keeps two tabs visible per pane without
/// truncating the fixture's ~25-character titles early.
pub(crate) const SPLIT_DOC_TAB_LABEL_COLS: usize = 14;

/// Plain-text marker prefixed to a preview tab's label (contract §1).
///
/// `∘ ` (U+2218 RING OPERATOR, one display column + one space). The preview
/// tab is *also* rendered italic via `TabItem::is_preview`, but a
/// `TestBackend` screen dump is symbols-only, so italic alone is not an
/// assertable fact. The marker is additive to the italic styling, never a
/// replacement for it, and is never itself truncated away — it pushes the
/// §2b budget out by its own 2 columns rather than eating into it.
pub(crate) const PREVIEW_MARKER: &str = "∘ ";

/// §4 (#2283) overflow affordances: baked into the leftmost/rightmost
/// *visible* tab's label when tabs exist beyond that edge of the strip.
///
/// Baked into label text for the same reason [`PREVIEW_MARKER`] and the
/// close glyph are (see [`doc_tab_label`]'s doc comment): quadraui's TUI
/// tab-bar rasteriser never paints scroll arrows itself —
/// `TuiBackend::draw_tab_bar` / `tab_bar_layout` hardcode
/// `scroll_arrow_width: 0.0` ("no scroll arrows in TUI") and simply honour
/// whatever `scroll_offset` the caller supplies — so the app has to paint
/// them. See `CoordApp::board_doc_tab_strip` (render.rs).
pub(crate) const SCROLL_LEFT_MARKER: char = '‹';
pub(crate) const SCROLL_RIGHT_MARKER: char = '›';

/// #2288 (contract §9): the glyph painted down the column that separates
/// two panes — `║` (U+2551 BOX DRAWINGS DOUBLE VERTICAL), one display
/// column wide.
///
/// Deliberately **not** the `│` (U+2502) quadraui's own `draw_split_tree`
/// rasteriser paints: this app already renders `│` at column 2 as the
/// sidebar/main boundary, so on a symbols-only grid a pane divider needs
/// its own code point to be unambiguous. §9 pins this one.
pub(crate) const PANE_DIVIDER_CHAR: char = '║';

/// Truncate `s` to at most `max_cols` display columns, appending `…` (which
/// occupies the last column) when anything was dropped.
///
/// Distinct from [`trunc`], which hard-cuts with no ellipsis: the contract's
/// pinned tab labels (`"#101 Fix login race…"`) carry the ellipsis *inside*
/// the 20-column budget, so a 26-column label becomes 19 columns of text plus
/// the marker, not 20 plus a 21st column.
pub(crate) fn truncate_with_ellipsis(s: &str, max_cols: usize) -> String {
    if s.chars().count() <= max_cols {
        return s.to_string();
    }
    if max_cols == 0 {
        return String::new();
    }
    format!("{}…", trunc(s, max_cols - 1))
}

/// The detail pane's sub-state for **one document** (#2285, contract §5).
///
/// Before #2285 these lived as single fields on `CoordApp`, so with three tabs
/// open all three shared one sub-tab, one scroll offset and one expanded
/// stage: scroll one tab's Issue body and the other two jumped. Holding them
/// per document is what makes the strip a set of tabs rather than one pane
/// with a strip above it.
///
/// # Scope ownership
///
/// A record belongs to exactly one [`PanelScope`] (it is stored inside that
/// scope's [`DocTabGroup`]), so only that scope's fields are ever read or
/// written on it — Board touches [`Self::board_tab`], Pipeline touches
/// [`Self::pipeline_tab`] / [`Self::stage_scroll`] / [`Self::focused_stage`],
/// and the two never see each other's. [`Self::scroll`] is the pane's body
/// scroll under either scope (`CoordApp::detail_scroll` for Board,
/// `pipeline_detail_scroll` for Pipeline) — the same *meaning*, a different
/// live field, which is why it is one field here and not two.
///
/// # Lifetime
///
/// Created lazily on the first tab switch away from a document (see
/// `CoordApp::checkpoint_detail_sub_state`) and dropped by
/// [`DocTabGroup::prune_sub_state`] the moment its tab leaves the strip —
/// closed, or evicted from the preview slot. Contract §5: "closing a tab
/// discards its sub-state; re-opening the same issue starts from the
/// defaults."
///
/// # Persistence (#2286, contract §6)
///
/// Only the *cheap* half survives a restart: [`Self::board_tab`] /
/// [`Self::pipeline_tab`] round-trip through [`PersistedDoc::sub_tab`].
/// [`Self::scroll`] / [`Self::stage_scroll`] / [`Self::focused_stage`] are
/// deliberately NOT written — they are positions inside a body whose content
/// has almost certainly moved on by the next launch, so restoring them would
/// point at the wrong place rather than at nothing.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DetailSubState {
    /// Board scope: which Board detail sub-tab this document is on.
    pub(crate) board_tab: BoardDetailTab,
    /// Pipeline scope: which Pipeline detail sub-tab this document is on.
    pub(crate) pipeline_tab: PipelineDetailTab,
    /// The detail pane body's scroll offset (Issue/Log/Summary bodies).
    pub(crate) scroll: usize,
    /// Pipeline scope: the Overview sub-tab's own stage-content scroll.
    pub(crate) stage_scroll: usize,
    /// Pipeline scope: the focused/expanded stage on the Overview sub-tab.
    pub(crate) focused_stage: Option<usize>,
}

/// One panel's ordered set of open documents.
///
/// Field invariants (upheld by every mutator, asserted by
/// [`Self::debug_check`]):
/// - `active` and `preview`, when `Some`, are valid indices into `tabs`.
/// - `active` is `Some` iff `tabs` is non-empty.
/// - at most one preview tab exists, and it is `tabs[preview]`.
/// - `sub_state` only ever holds keys that are still in `tabs` (#2285).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DocTabGroup {
    tabs: Vec<DocKey>,
    active: Option<usize>,
    preview: Option<usize>,
    /// #2285: per-document detail sub-state, keyed by the same [`DocKey`] the
    /// strip is ordered by. Sparse — a document that has never been switched
    /// away from has no entry and reads as [`DetailSubState::default`].
    sub_state: HashMap<DocKey, DetailSubState>,
}

impl DocTabGroup {
    /// The open documents, in strip order.
    pub(crate) fn tabs(&self) -> &[DocKey] {
        &self.tabs
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.tabs.is_empty()
    }

    /// Index of the active tab, or `None` when nothing is open.
    pub(crate) fn active_index(&self) -> Option<usize> {
        self.active
    }

    /// The active document's key, or `None` when nothing is open.
    pub(crate) fn active_key(&self) -> Option<&DocKey> {
        self.active.and_then(|i| self.tabs.get(i))
    }

    /// Whether the tab at `idx` is the (single) preview tab.
    pub(crate) fn is_preview(&self, idx: usize) -> bool {
        self.preview == Some(idx)
    }

    /// Index of `key` in the strip, if it is open.
    pub(crate) fn index_of(&self, key: &DocKey) -> Option<usize> {
        self.tabs.iter().position(|k| k == key)
    }

    // ── #2285 (ms-65 §5): per-document detail sub-state ──────────────────

    /// `key`'s stored sub-state, or `None` for a document that has never been
    /// switched away from (callers read that as [`DetailSubState::default`] —
    /// contract §5's "defaults on open match today's defaults").
    pub(crate) fn sub_state(&self, key: &DocKey) -> Option<&DetailSubState> {
        self.sub_state.get(key)
    }

    /// Store `state` against `key`. A no-op for a key that is not open, so a
    /// checkpoint racing a close can never resurrect discarded sub-state.
    pub(crate) fn set_sub_state(&mut self, key: DocKey, state: DetailSubState) {
        if self.index_of(&key).is_some() {
            self.sub_state.insert(key, state);
        }
    }

    /// Contract §5: a tab that leaves the strip takes its sub-state with it.
    ///
    /// Called from every mutator that can *remove* a key — [`Self::close`] and
    /// [`Self::open_preview`]'s replace-in-place branch (which evicts the
    /// previous preview document just as surely as a close does). Re-opening
    /// the same issue number afterwards therefore starts from the defaults.
    fn prune_sub_state(&mut self) {
        if self.sub_state.is_empty() {
            return;
        }
        self.sub_state.retain(|k, _| self.tabs.contains(k));
    }

    /// Contract §2e rules 1/2/4 — the single-click path.
    ///
    /// Already open → activate it, unchanged. Else a preview exists → replace
    /// it **in place** (same index, same active state). Else → append a new
    /// preview tab and activate it.
    ///
    /// Deliberately does *not* promote: rule 2 says an already-open tab is
    /// activated with "no new tab, no replace", and promoting the preview here
    /// would make the very next single click append instead of replace,
    /// breaking rule 4's "at most one preview tab per tab group, ever".
    pub(crate) fn open_preview(&mut self, key: DocKey) {
        if let Some(idx) = self.index_of(&key) {
            self.active = Some(idx);
        } else if let Some(slot) = self.preview {
            self.tabs[slot] = key;
            self.active = Some(slot);
        } else {
            self.tabs.push(key);
            let idx = self.tabs.len() - 1;
            self.preview = Some(idx);
            self.active = Some(idx);
        }
        // #2285: the replace-in-place branch above evicted a document — its
        // sub-state goes with it, exactly as a close would discard it.
        self.prune_sub_state();
        self.debug_check();
    }

    /// Contract §2e rule 3 — the double-click path: open-or-activate exactly as
    /// [`Self::open_preview`] does, **then** promote the resulting tab to
    /// pinned.
    pub(crate) fn pin(&mut self, key: DocKey) {
        self.open_preview(key);
        self.promote_active();
    }

    /// Drop the active tab out of the preview slot, if it is in it. A no-op
    /// when the active tab is already pinned or nothing is open.
    pub(crate) fn promote_active(&mut self) {
        if self.preview.is_some() && self.preview == self.active {
            self.preview = None;
        }
        self.debug_check();
    }

    /// Activate the tab at `idx` from the tab strip itself.
    ///
    /// This is the milestone's **promote-on-select** trigger: selecting a
    /// preview tab directly (as opposed to re-opening its document from the
    /// sidebar, which rule 2 leaves untouched) is a deliberate "I want to keep
    /// this one" gesture, so it pins the tab. Returns `true` when anything
    /// changed, so the caller can decide whether a redraw is needed.
    pub(crate) fn activate_index(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let changed = self.active != Some(idx) || self.preview == Some(idx);
        self.active = Some(idx);
        self.promote_active();
        changed
    }

    /// Contract §4 (#2283) — close the tab at `idx`. Returns `false` (no-op)
    /// when `idx` is out of range.
    ///
    /// **Active-neighbour rule**, pinned by the contract: closing the
    /// ACTIVE tab activates the tab immediately to its left, or — when the
    /// closed tab was the leftmost — the new leftmost. Both branches reduce
    /// to the same arithmetic: the tab that was at `idx.saturating_sub(1)`
    /// keeps that index after the removal (elements before `idx` are
    /// untouched by `Vec::remove(idx)`), so `active` becomes
    /// `idx.saturating_sub(1)` whenever `idx` was active. Closing the LAST
    /// remaining tab clears `active` to `None` (§4's empty state, #2283).
    ///
    /// Closing an INACTIVE tab leaves the active *document* unchanged: the
    /// active index is only re-based (decremented) when the closed tab sat
    /// to its left, never retargeted.
    ///
    /// The (single) preview slot follows the same re-basing: cleared if the
    /// closed tab WAS the preview, decremented if the preview sat to the
    /// closed tab's right, else untouched.
    pub(crate) fn close(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let was_active = self.active == Some(idx);
        self.tabs.remove(idx);

        self.preview = match self.preview {
            Some(p) if p == idx => None,
            Some(p) if p > idx => Some(p - 1),
            other => other,
        };

        if self.tabs.is_empty() {
            self.active = None;
        } else if was_active {
            self.active = Some(idx.saturating_sub(1));
        } else if let Some(a) = self.active {
            if a > idx {
                self.active = Some(a - 1);
            }
        }
        // #2285 (§5): closing a tab discards its sub-state.
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close others" item —
    /// close every tab except the one at `idx`, which becomes the sole
    /// survivor at index 0 and the active tab. Returns `false` (no-op) when
    /// `idx` is out of range.
    ///
    /// Preserves `idx`'s preview-ness rather than always clearing it: §8c
    /// only pins the effect on tab count/order, and collapsing to a single
    /// tab that was already the preview should not silently promote it —
    /// that is "Pin tab"'s job, not "Close others"'s.
    pub(crate) fn close_others(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let was_preview = self.preview == Some(idx);
        let survivor = self.tabs[idx].clone();
        self.tabs = vec![survivor];
        self.active = Some(0);
        self.preview = if was_preview { Some(0) } else { None };
        // #2285 (§5): every closed tab's sub-state goes with it.
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close all" item — close
    /// every open tab, reaching "the same end state as closing the last
    /// tab" (contract §8c / §4's empty state, #2283). Returns `false`
    /// (no-op) when nothing is open.
    pub(crate) fn close_all(&mut self) -> bool {
        if self.tabs.is_empty() {
            return false;
        }
        self.tabs.clear();
        self.active = None;
        self.preview = None;
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Pin tab" item — promote
    /// the tab at `idx` out of the preview slot, without changing which tab
    /// is active or the strip's order.
    ///
    /// Distinct from [`Self::promote_active`] (§2e rule 3's click-driven
    /// path, which only ever acts on the ACTIVE tab): the context menu's
    /// "Pin tab" targets the RIGHT-CLICKED tab, which need not be the
    /// active one. A no-op — contract §8c's "inert" reading of "hidden or
    /// inert" on an already-pinned tab — when `idx` is not the (single)
    /// preview tab, whether because it's already pinned or out of range.
    pub(crate) fn promote(&mut self, idx: usize) -> bool {
        if self.preview == Some(idx) {
            self.preview = None;
            self.debug_check();
            true
        } else {
            false
        }
    }

    #[inline]
    fn debug_check(&self) {
        debug_assert!(
            self.active.map_or(self.tabs.is_empty(), |i| i < self.tabs.len()),
            "active index must point at a live tab: {self:?}"
        );
        debug_assert!(
            self.preview.map_or(true, |i| i < self.tabs.len()),
            "preview index must point at a live tab: {self:?}"
        );
    }

    // ── #2286 (ms-65 §6): persistence — drop dead documents on load ─────

    /// Contract §6 bullets 2/3: drop every tab whose key is not in `known` —
    /// e.g. its issue was closed and dropped from the board since this group
    /// was last persisted. "A surviving neighbour becomes active" and "if
    /// none survive, start with no tabs" both fall out of [`Self::close`]'s
    /// own already-tested active-neighbour rebasing, applied once per dead
    /// tab, rather than a second hand-rolled "pick a neighbour" algorithm:
    /// closing the tab at its own index rebases `active`/`preview` and
    /// prunes `sub_state` for it exactly as a live `Ctrl-W` close would.
    ///
    /// Walks indices highest-to-lowest so each `close(idx)` call still
    /// targets the right (not-yet-shifted) tab — [`Vec::remove`] only moves
    /// elements *after* the removed index, so indices below the one being
    /// processed are never disturbed by an earlier iteration.
    pub(crate) fn retain_known(&mut self, known: &HashSet<DocKey>) {
        for idx in (0..self.tabs.len()).rev() {
            if !known.contains(&self.tabs[idx]) {
                self.close(idx);
            }
        }
    }

    /// Rebuild a group from its persisted form ([`PersistedScope`]), trusting
    /// the file as-is. Pruning against the live board is the separate
    /// [`Self::retain_known`] step — at real startup (`CoordApp::new`) the
    /// board hasn't loaded yet, and pruning against an empty known-set would
    /// throw away everything this function just restored before the first
    /// real data tick ever reconciles it (mirrors `Workspace::load` /
    /// `sync_workspace_repos`, `app/workspace.rs`).
    fn from_persisted(scope: PersistedScope, owner: PanelScope) -> Self {
        // #2286 review (non-blocking 2): de-duplicate on the way in. The
        // writer never emits the same `{repo, issue}` twice, but the file is
        // user-visible and hand-editable, and a duplicate would otherwise
        // render as two identical tabs that `index_of` can only ever resolve
        // to the first of — so `close`ing the second one is unreachable and
        // activating either always lights up the left one. First occurrence
        // wins, which is also what `index_of` would have picked.
        let mut seen: HashSet<DocKey> = HashSet::new();
        let mut tabs: Vec<DocKey> = Vec::with_capacity(scope.tabs.len());
        let mut sub_state = HashMap::new();
        for doc in &scope.tabs {
            let key = doc.key();
            if !seen.insert(key.clone()) {
                continue;
            }
            if let Some(state) = doc.sub_state(owner) {
                sub_state.insert(key.clone(), state);
            }
            tabs.push(key);
        }
        let mut group = DocTabGroup {
            tabs,
            active: None,
            preview: None,
            sub_state,
        };
        group.active = scope.active.as_ref().and_then(|d| group.index_of(&d.key()));
        group.preview = scope.preview.as_ref().and_then(|d| group.index_of(&d.key()));
        if group.active.is_none() && !group.tabs.is_empty() {
            // A malformed-but-parseable file (e.g. a `null` active alongside
            // a non-empty `tabs`, or an `active` key that isn't itself in
            // `tabs`) would otherwise violate `debug_check`'s "active is Some
            // iff tabs is non-empty" invariant for the rest of the group's
            // life. Anchor on the leftmost tab rather than carry that in.
            group.active = Some(0);
        }
        group.debug_check();
        group
    }

    /// This group's persisted form ([`PersistedScope`], contract §6).
    fn to_persisted(&self, owner: PanelScope) -> PersistedScope {
        let doc = |key: &DocKey| PersistedDoc::from_key(key, self.sub_state.get(key), owner);
        PersistedScope {
            tabs: self.tabs.iter().map(&doc).collect(),
            active: self.active_key().map(&doc),
            preview: self.preview.and_then(|i| self.tabs.get(i)).map(&doc),
        }
    }
}

// ═════════════════════════════════════════════════════════════════════════
// #2288 (ms-65 §9): side-by-side split — two tab groups in one panel
// ═════════════════════════════════════════════════════════════════════════

/// The scope's panes and the [`SplitTree`] that arranges them.
///
/// #2288 applies #2282's per-scope model **one level deeper**: a scope owns
/// an ordered list of panes, each of which is a whole [`DocTabGroup`] (its
/// own tab order, its own active tab, its own single preview slot, its own
/// per-document sub-state). Exactly one pane is focused; every existing
/// mutation path — open, pin, close, cycle — reaches the focused pane
/// through [`DocTabs::group`] / [`DocTabs::group_mut`] and so needs no
/// change at all.
///
/// # Invariants
///
/// - `panes` is never empty (contract §9: "a scope always has ≥1 pane", the
///   reason [`Self::close_pane`] is a no-op at one pane).
/// - `focused` is always a valid index into `panes`.
/// - With **one** pane the whole type is inert: [`Self::split_tree`] is a
///   bare `Leaf`, nothing paints a divider, and the rendering is
///   byte-identical to pre-#2288 (contract §9's last bullet).
///
/// # Why the tree is derived, not stored
///
/// `ratio` is the only mutable state a two-leaf tree has, so holding a
/// `SplitTree` field as well would be a second source of truth for the same
/// number (and would cost `DocTabs` its `Eq`, since `SplitTree` carries an
/// `f32`). [`Self::split_tree`] builds the tree from `panes.len()` + `ratio`
/// on demand, and every geometry question — leaf rects, divider column,
/// divider hit-test, drag-to-ratio — is answered by asking that tree via
/// quadraui's own `layout()` / `hit_test_*`, never by hand-rolled math.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PaneSet {
    panes: Vec<DocTabGroup>,
    focused: usize,
    ratio: f32,
}

impl Default for PaneSet {
    fn default() -> Self {
        Self {
            panes: vec![DocTabGroup::default()],
            focused: 0,
            ratio: DEFAULT_SPLIT_RATIO,
        }
    }
}

/// The 50/50 split contract §9 pins as the default ("At the default 50/50
/// split of the 82-column main panel…").
pub(crate) const DEFAULT_SPLIT_RATIO: f32 = 0.5;

/// Stable per-pane leaf id for the [`SplitTree`]. Only ever consumed by
/// `SplitTreeLayout`'s own leaf lookup, so the exact string is private
/// detail — but it must be *stable* across frames, since the layout is
/// recomputed from scratch on every paint and every hit-test.
fn pane_leaf_id(scope: PanelScope, idx: usize) -> quadraui::WidgetId {
    let scope = match scope {
        PanelScope::Board => "board",
        PanelScope::Pipeline => "pipeline",
    };
    quadraui::WidgetId::new(format!("doc-pane:{scope}:{idx}"))
}

impl PaneSet {
    /// An unsplit scope holding `group`.
    pub(crate) fn single(group: DocTabGroup) -> Self {
        Self {
            panes: vec![group],
            focused: 0,
            ratio: DEFAULT_SPLIT_RATIO,
        }
    }

    /// True once the scope holds more than one pane — i.e. a `║` divider is
    /// painted and the narrower §9 label budget applies.
    pub(crate) fn is_split(&self) -> bool {
        self.panes.len() > 1
    }

    pub(crate) fn focused_index(&self) -> usize {
        self.focused
    }

    pub(crate) fn focused(&self) -> &DocTabGroup {
        &self.panes[self.focused]
    }

    pub(crate) fn focused_mut(&mut self) -> &mut DocTabGroup {
        let idx = self.focused;
        &mut self.panes[idx]
    }

    /// The pane at `idx`, or the focused pane when `idx` is out of range —
    /// the render path addresses panes positionally and must never panic on
    /// a stale index left over from a frame taken before a collapse.
    pub(crate) fn pane(&self, idx: usize) -> &DocTabGroup {
        self.panes.get(idx).unwrap_or_else(|| self.focused())
    }

    /// How many panes this scope currently holds — always ≥ 1.
    pub(crate) fn len(&self) -> usize {
        self.panes.len()
    }

    /// Mutable [`Self::pane`], with the same out-of-range rule: a stale
    /// index falls back to the focused pane rather than panicking.
    pub(crate) fn pane_mut(&mut self, idx: usize) -> &mut DocTabGroup {
        let idx = if idx < self.panes.len() {
            idx
        } else {
            self.focused
        };
        &mut self.panes[idx]
    }

    /// Contract §9 `Ctrl-W v`: split the focused pane right. The new pane is
    /// empty and takes focus — the mock settles this
    /// (`mocks/board-split-side-by-side.screen` shows the post-split single
    /// click landing in the *right* pane's preview slot, and §2e rule 1 says
    /// a single click targets the focused group).
    ///
    /// Returns `false` when the scope is already split: ms-65 ships
    /// side-by-side only, two panes maximum (the tracking issue's "Out of
    /// scope: 2×2 quadrants" line). The `Vec` + tree shape is what lets a
    /// later milestone lift that cap without a redesign.
    pub(crate) fn split_focused(&mut self) -> bool {
        if self.is_split() {
            return false;
        }
        self.panes.insert(self.focused + 1, DocTabGroup::default());
        self.focused += 1;
        self.ratio = DEFAULT_SPLIT_RATIO;
        true
    }

    /// The whole scope flattened into ONE [`DocTabGroup`], for persistence.
    ///
    /// #2288 review (blocking, carried from round 2): the persisted file
    /// shape is per-**scope** (contract §6 — widening it to per-pane would be
    /// a Gate-A amendment, not an implementation choice), but writing only
    /// `focused()` made the split *lossy*: split the Board panel, open or pin
    /// issues in the new pane, quit — and every tab in the unfocused pane was
    /// silently gone from `~/.coord/tabs.json` on the next launch. Silent
    /// data loss on a plain quit is worse than the restore fidelity the
    /// per-scope shape costs us, so the writer unions every pane instead.
    ///
    /// What the union preserves and what it flattens:
    ///
    /// - **tabs**: every pane's documents, in pane order (left → right) and
    ///   strip order within a pane, de-duplicated by [`DocKey`] — the same
    ///   first-occurrence-wins rule [`DocTabGroup::from_persisted`] applies on
    ///   the way back in. The same issue open in both panes is one document in
    ///   the file, which is all the shape can hold.
    /// - **sub-state**: taken from the pane the document first appears in, so
    ///   a document only open in the *unfocused* pane still restores onto its
    ///   own sub-tab rather than the scope default.
    /// - **active / preview**: the focused pane's, since that is the document
    ///   the user was actually looking at on the way out. `preview` falls back
    ///   to the first pane that has one so a lone preview in the other pane
    ///   comes back italic rather than silently promoted.
    ///
    /// With **one** pane this is `focused().clone()` and the bytes written are
    /// identical to the pre-split writer — every #2286 scenario included.
    pub(crate) fn merged_for_persist(&self) -> DocTabGroup {
        if !self.is_split() {
            return self.focused().clone();
        }
        let focused = self.focused();
        let active_key = focused.active_key().cloned();
        let preview_key = focused
            .preview
            .and_then(|i| focused.tabs.get(i))
            .or_else(|| {
                self.panes
                    .iter()
                    .find_map(|p| p.preview.and_then(|i| p.tabs.get(i)))
            })
            .cloned();

        let mut merged = DocTabGroup::default();
        let mut seen: HashSet<DocKey> = HashSet::new();
        for pane in &self.panes {
            for key in &pane.tabs {
                if !seen.insert(key.clone()) {
                    continue;
                }
                if let Some(state) = pane.sub_state.get(key) {
                    merged.sub_state.insert(key.clone(), state.clone());
                }
                merged.tabs.push(key.clone());
            }
        }
        merged.active = active_key.and_then(|k| merged.index_of(&k));
        merged.preview = preview_key.and_then(|k| merged.index_of(&k));
        if merged.active.is_none() && !merged.tabs.is_empty() {
            // The focused pane can legitimately be empty while another pane
            // holds tabs (`Ctrl-W v` then quit without opening anything).
            // `debug_check` still demands "active is Some iff tabs is
            // non-empty", so anchor on the leftmost surviving document —
            // exactly what `from_persisted` does for a malformed file.
            merged.active = Some(0);
        }
        merged.debug_check();
        merged
    }

    /// Contract §9 `Ctrl-W w`: move focus to the next pane, wrapping.
    /// Returns `false` (no-op) at one pane.
    pub(crate) fn focus_next(&mut self) -> bool {
        if !self.is_split() {
            return false;
        }
        self.focused = (self.focused + 1) % self.panes.len();
        true
    }

    /// Focus the pane at `idx` (a mouse click inside it). Returns `true`
    /// when focus actually moved.
    pub(crate) fn set_focused(&mut self, idx: usize) -> bool {
        if idx >= self.panes.len() || idx == self.focused {
            return false;
        }
        self.focused = idx;
        true
    }

    /// Contract §9 `Ctrl-W x`: close the focused pane. A **no-op at one
    /// pane** — "Closing the last remaining pane in a scope is a no-op (or
    /// disabled) — a scope always has ≥1 pane."
    ///
    /// The surviving pane keeps its own tabs untouched; the closed pane's
    /// tabs (and their sub-state) go with it, exactly as closing a tab
    /// discards that document's sub-state.
    pub(crate) fn close_focused_pane(&mut self) -> bool {
        if !self.is_split() {
            return false;
        }
        self.panes.remove(self.focused);
        self.focused = self.focused.min(self.panes.len() - 1);
        true
    }

    /// Contract §9: "Closing the last tab in one pane collapses that pane
    /// back to a single-pane layout."
    ///
    /// Called only from the *close* paths — never after
    /// [`Self::split_focused`], whose whole point is a freshly-created empty
    /// pane that must survive until a document is opened into it.
    ///
    /// Returns `true` when the layout actually collapsed.
    pub(crate) fn collapse_empty_panes(&mut self) -> bool {
        if !self.is_split() {
            return false;
        }
        let before = self.panes.len();
        // Highest-to-lowest so surviving indices (and the focus fix-up
        // below) are never disturbed mid-walk. Stop at one pane: a scope
        // with every pane empty collapses to a single empty pane, which is
        // exactly the zero-tab baseline (§2a / §4's empty state).
        for idx in (0..self.panes.len()).rev() {
            if self.panes.len() == 1 {
                break;
            }
            if self.panes[idx].is_empty() {
                self.panes.remove(idx);
                if self.focused > idx {
                    self.focused -= 1;
                }
            }
        }
        self.focused = self.focused.min(self.panes.len() - 1);
        self.panes.len() != before
    }

    /// Store a dragged divider ratio, clamped by the primitive itself —
    /// `SplitTree::set_ratio_at_index` applies quadraui's own
    /// `MIN_RATIO`..=`MAX_RATIO` bounds, so neither pane can ever be dragged
    /// to zero width and this app never re-derives that clamp.
    pub(crate) fn set_ratio(&mut self, scope: PanelScope, ratio: f32) -> bool {
        if !self.is_split() {
            return false;
        }
        let mut tree = self.split_tree(scope);
        if !tree.set_ratio_at_index(0, ratio) {
            return false;
        }
        let clamped = tree.ratio_at_index(0).unwrap_or(self.ratio);
        if (clamped - self.ratio).abs() < f32::EPSILON {
            return false;
        }
        self.ratio = clamped;
        true
    }

    /// This scope's layout as a quadraui [`SplitTree`].
    ///
    /// **`SplitDirection::Horizontal` is deliberate and is the one thing in
    /// this file that must not be guessed at.** quadraui's `Horizontal`
    /// means *panes side-by-side, first = left*
    /// (`primitives/split.rs:40-49`); vimcode's identically-named variant
    /// means *split top/bottom*. §9 and #2288's own ⚠ both call this out
    /// because the wrong choice compiles, type-checks and silently renders
    /// the panes stacked. The acceptance slice asserts the orientation
    /// against the rendered grid, not against this name.
    ///
    /// One pane ⇒ a bare `Leaf`, i.e. no divider and no geometry change:
    /// contract §9's "with a single pane, rendering is byte-identical".
    pub(crate) fn split_tree(&self, scope: PanelScope) -> SplitTree {
        let mut tree = SplitTree::leaf(pane_leaf_id(scope, 0));
        for idx in 1..self.panes.len() {
            tree = SplitTree::split(
                SplitDirection::Horizontal,
                self.ratio,
                tree,
                SplitTree::leaf(pane_leaf_id(scope, idx)),
            );
        }
        tree
    }
}

/// Every panel's document tabs, keyed by scope.
///
/// Each scope holds a [`PaneSet`] rather than a bare [`DocTabGroup`]
/// (#2288): with one pane — the default, and the only state #2282–#2287
/// ever reach — the two are indistinguishable, since [`Self::group`] hands
/// back that single pane's group.
#[derive(Debug, Clone, Default)]
pub(crate) struct DocTabs {
    board: PaneSet,
    pipeline: PaneSet,
    /// The `~/.coord/tabs.json` this instance persists to, resolved ONCE by
    /// [`Self::load`] and never re-derived from the ambient `HOME`
    /// afterwards — see [`Self::path`]'s "pinned at construction" section
    /// for why re-reading `HOME` per save is a live cross-test hazard.
    ///
    /// `None` — the default, and what every explicit-path constructor
    /// ([`Self::load_from_path`], [`Self::default`]) yields — means "this
    /// instance has no ambient file": [`Self::save`] / [`Self::save_if_exists`]
    /// are no-ops, and only the explicit [`Self::save_to_path`] writes.
    origin: Option<PathBuf>,
}

/// Deliberately hand-written rather than derived: [`DocTabs::origin`] is
/// *where* a tab set persists, not part of the tab set itself, so two
/// instances holding the same tabs compare equal whether one of them came
/// from disk and the other was built in memory.
impl PartialEq for DocTabs {
    fn eq(&self, other: &Self) -> bool {
        self.board == other.board && self.pipeline == other.pipeline
    }
}

impl DocTabs {
    /// The **focused** pane's tab group for `scope` — what every open /
    /// pin / close / cycle path acts on, and what the status bar and
    /// persistence report. Identical to the whole scope's group whenever
    /// the scope is unsplit.
    pub(crate) fn group(&self, scope: PanelScope) -> &DocTabGroup {
        self.panes(scope).focused()
    }

    pub(crate) fn group_mut(&mut self, scope: PanelScope) -> &mut DocTabGroup {
        self.panes_mut(scope).focused_mut()
    }

    /// #2288: `scope`'s panes and split geometry.
    pub(crate) fn panes(&self, scope: PanelScope) -> &PaneSet {
        match scope {
            PanelScope::Board => &self.board,
            PanelScope::Pipeline => &self.pipeline,
        }
    }

    pub(crate) fn panes_mut(&mut self, scope: PanelScope) -> &mut PaneSet {
        match scope {
            PanelScope::Board => &mut self.board,
            PanelScope::Pipeline => &mut self.pipeline,
        }
    }

    /// Contract §6 bullets 2/3, applied to both scopes at once — the
    /// `CoordApp` integration point (`mod.rs`'s `sync_doc_tabs` for real
    /// startup, `fixtures.rs`'s `make_test_app` for the fixture path) calls
    /// this once real board data is known.
    ///
    /// #2288: every pane of every scope, not just the focused one — a
    /// document whose issue has left the board must not survive in a
    /// background pane.
    pub(crate) fn retain_known(&mut self, known: &HashSet<DocKey>) {
        for scope in [PanelScope::Board, PanelScope::Pipeline] {
            for group in self.panes_mut(scope).panes.iter_mut() {
                group.retain_known(known);
            }
        }
    }

    // ─── Persistence: `~/.coord/tabs.json` ──────────────────────────────

    /// Path to the persisted tabs file (`~/.coord/tabs.json`), or `None`
    /// when `HOME` is unset — matches `TuiSettings::path()` / `Workspace::path()`.
    ///
    /// # Test / `test-support` builds only: real, un-sandboxed `HOME` is refused
    ///
    /// `DocTabs` is unlike every sibling `~/.coord/*` persistence type in one
    /// respect: [`DocTabs::retain_known`] means a correct restore can only be
    /// observed by actually constructing a `CoordApp` from a fixture that
    /// already knows the board (`fixtures.rs::make_test_app`'s doc comment —
    /// and `tests/acceptance/ms-65/manifest.yml` finding 14 — explain why
    /// this is unavoidable), so — unlike `Workspace`'s fixtures, which derive
    /// fresh and never touch disk — that fixture path genuinely calls
    /// [`DocTabs::load`]. And unlike `Workspace`'s CoordApp-integration
    /// methods (`open_project`/`close_project`/…), which no existing test
    /// exercises, this crate's #2282-#2287 TuiDriver suites exercise
    /// `open_board_doc_tab`/`activate_board_doc_tab`/… (and therefore
    /// [`DocTabs::save_if_exists`]) *extensively* — hundreds of clicks across
    /// dozens of tests, none of which sandbox `HOME`.
    ///
    /// With no injectable `~/.coord` seam (`tests/acceptance/ms-65/
    /// manifest.yml` finding 14b — a repo-wide fix flagged there as the
    /// coordinator's follow-up, not this issue's), an unguarded `path()`
    /// would mean every one of those clicks reads and writes the REAL
    /// developer's `~/.coord/tabs.json` — verified while authoring this: it
    /// self-pollutes within a single `cargo test` process (one test's writes
    /// leak into the very next test's `DocTabs::load()`, since both hit the
    /// same real file) and even a single `q`-quit test creates the file for
    /// every other unsandboxed test to trip over afterward.
    ///
    /// The guard: `tests/acceptance/ms-65/tabs_persistence_2286.rs`'s own
    /// `HomeSandbox` (and any other test that wants real doc-tabs I/O)
    /// points `HOME` at a fresh directory under [`std::env::temp_dir`] — an
    /// ordinary developer or CI `$HOME` is never there. So under `#[cfg(test)]`
    /// / `test-support` only, `path()` additionally requires `HOME` to
    /// resolve inside the system temp directory; otherwise it refuses (same
    /// as `HOME` being unset) rather than touching a real home directory.
    /// Compiled out entirely for the shipped binary — real users are
    /// unaffected, exactly like `Workspace`/`TuiSettings` today.
    ///
    /// # This is resolved ONCE, at construction — never per save
    ///
    /// The guard above is a property of the PROCESS, not of the test that
    /// installed the sandbox. `HOME` is process-global, so the moment one
    /// test points it at a temp dir, EVERY other test running concurrently
    /// in the same binary starts resolving that same sandbox — and the
    /// acceptance binary runs ~110 tests across as many threads as the host
    /// has cores. `tests/acceptance/ms-65/tabs_persistence_2286.rs`'s
    /// `HomeSandbox` serialises its own slice against itself (its
    /// `HOME_LOCK`), which is all it can do from inside one module; it
    /// cannot serialise the other slices sharing the binary.
    ///
    /// That is why [`Self::load`] pins the answer into [`Self::origin`] and
    /// [`Self::save`] / [`Self::save_if_exists`] use the pinned value
    /// instead of calling this again. An app built while `HOME` was the
    /// developer's real home has `origin == None` *for its whole life*, so
    /// a sandbox installed by some other thread half a second later can
    /// never capture its writes. Without the pin, exactly that happened:
    /// every unsandboxed app's tab clicks were redirected into whichever
    /// sandbox happened to be live at that instant, and the sandboxing
    /// test lost its own `tabs.json` to a neighbour (#2288 — reproducible
    /// on essentially every full-suite run once the `board_split_2288`
    /// slice started driving tab strips for its full length instead of
    /// panicking on its first assertion).
    ///
    /// The residue this does NOT fix is an app *constructed* inside another
    /// thread's sandbox window; that needs the injectable `~/.coord` seam
    /// `tests/acceptance/ms-65/manifest.yml` finding 14b flags as a
    /// repo-wide follow-up, or a single-threaded acceptance binary.
    ///
    /// # Why the residue cannot be closed from inside this crate
    ///
    /// The residue narrows to ONE interleaving: a foreign thread calls
    /// `claim_origin` for a sandbox in the gap between `HomeSandbox::new`'s
    /// `set_var("HOME", …)` and the owning test's own `make_test_app`
    /// (`restart_with` spends one `fs::write` there, so ~0.2 ms of a ~70 ms
    /// sandbox window). First-loader-wins then names the foreign thread, so
    /// it reads the seeded `tabs.json` its own test never wrote *and* the
    /// owner is refused its own file. A foreign thread that arrives any
    /// LATER in the window is already handled correctly — it is refused and
    /// falls back to `origin == None`.
    ///
    /// That gap is not closable here, because at claim time the owner and a
    /// foreign thread are indistinguishable from process state: `HOME` is
    /// per-PROCESS, not per-thread, so both observe the identical value, the
    /// identical sandbox directory and the identical seeded file, and
    /// nothing in `std` reports which thread called `set_var`. Any fix needs
    /// cooperation from the sandbox (the injectable seam finding 14b asks
    /// for) or serialisation of the whole binary. Timing heuristics — claim
    /// only within N ms of the sandbox directory's creation — were
    /// considered and rejected: they merely swap a foreign thread's silent
    /// pollution for the owner's own test failing when it is descheduled
    /// past the threshold.
    ///
    /// Measured on this branch at `HEAD`, over 70 full runs of the sealed
    /// ms-65 + ms-38 acceptance binary (30 sequential, then 40 as four
    /// concurrent processes on a 20-core host): 3 residue events, all in
    /// `tabs_persistence_2286` / `tabs_discoverability_2287`, and 0 in
    /// `board_split_2288` — which is 17/17 green across all 70, plus four
    /// `coord acceptance run --issue 2288` invocations. The 40 runs under
    /// 4× oversubscription produced no residue event at all.
    pub(crate) fn path() -> Option<PathBuf> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        #[cfg(any(test, feature = "test-support"))]
        {
            if !home.starts_with(std::env::temp_dir()) {
                return None;
            }
        }
        Some(home.join(".coord").join("tabs.json"))
    }

    /// Load from a specific path, trusting the file as-is (no pruning —
    /// see [`Self::retain_known`]). Contract §6 bullet 4: a missing file, an
    /// empty file, or a file that fails to parse all produce the SAME
    /// result — every scope starts with no tabs, never a panic, never a
    /// partial/best-effort parse. `serde_json::from_str` already rejects a
    /// truncated document wholesale (no partial-object recovery), so the
    /// malformed and empty cases fall out of the same `Err` branch as a
    /// missing file falls out of `read_to_string`'s `Err`.
    pub(crate) fn load_from_path(path: &std::path::Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        match serde_json::from_str::<PersistedTabs>(&text) {
            // #2288: a restored scope always comes back **unsplit** — the
            // file's shape (contract §6) is per-scope, not per-pane, and
            // widening it would be a Gate-A amendment rather than an
            // implementation choice. The restored group lands in the single
            // pane, which is exactly what `group()` reports.
            Ok(persisted) => Self {
                board: PaneSet::single(DocTabGroup::from_persisted(
                    persisted.board.unwrap_or_default(),
                    PanelScope::Board,
                )),
                pipeline: PaneSet::single(DocTabGroup::from_persisted(
                    persisted.pipeline.unwrap_or_default(),
                    PanelScope::Pipeline,
                )),
                // An explicit-path load pins nothing: only `load()` (which
                // resolved the path from `HOME` in the first place) sets
                // `origin`. See the field's doc comment.
                origin: None,
            },
            Err(_) => Self::default(),
        }
    }

    /// Load from `~/.coord/tabs.json`. Returns the default (empty) tab set
    /// when `HOME` is unset, the file is absent, or it fails to parse.
    ///
    /// Also PINS the resolved path into [`Self::origin`] — including when
    /// the file does not exist yet, so a first run still creates it on the
    /// way out — and that pinned value is what every later save uses. See
    /// [`Self::path`]'s "resolved ONCE" section.
    pub(crate) fn load() -> Self {
        let origin = Self::path().and_then(Self::claim_origin);
        let mut tabs = match &origin {
            Some(path) => Self::load_from_path(path),
            None => Self::default(),
        };
        tabs.origin = origin;
        tabs
    }

    /// Production: every resolved path is the caller's own — there is one
    /// app per process and one `~/.coord/tabs.json` per user, so this is
    /// the identity function and compiles away.
    #[cfg(not(any(test, feature = "test-support")))]
    fn claim_origin(path: PathBuf) -> Option<PathBuf> {
        Some(path)
    }

    /// Test / `test-support` builds: give each sandboxed `~/.coord/tabs.json`
    /// a single owning THREAD, and refuse it to every other one.
    ///
    /// This is the second half of the fix [`Self::path`] describes. Pinning
    /// [`Self::origin`] at construction stops an app that was built under
    /// the real `HOME` from ever writing into a sandbox some other thread
    /// installs later — but an app *constructed* while another thread's
    /// sandbox is live still resolves that sandbox, loads its seeded
    /// `tabs.json` (rendering a tab strip its own test never opened) and
    /// then overwrites it on the next click.
    ///
    /// A sandbox path is unique per test — `HomeSandbox` mixes a process id
    /// and a monotonic sequence number into the directory name — and its
    /// owner is, in every real ordering, the thread that constructs an app
    /// immediately after installing it. First-loader-wins therefore names
    /// the right thread, and the window in which another thread could get
    /// there first shrinks from "the whole test body" to "the few
    /// microseconds between `set_var("HOME")` and the owning test's own
    /// `make_test_app`".
    ///
    /// Keyed by thread rather than by instance so that a test which
    /// restarts its app — build a driver, seed a different `tabs.json`,
    /// build another — keeps its own file across both.
    #[cfg(any(test, feature = "test-support"))]
    fn claim_origin(path: PathBuf) -> Option<PathBuf> {
        use std::collections::hash_map::Entry;
        use std::sync::{Mutex, OnceLock};
        use std::thread::ThreadId;

        static OWNERS: OnceLock<Mutex<HashMap<PathBuf, ThreadId>>> = OnceLock::new();

        let owners = OWNERS.get_or_init(|| Mutex::new(HashMap::new()));
        // A poisoned lock only means some other test panicked while holding
        // it; the map is still consistent, and refusing to hand out any
        // path from here on would turn one failure into a cascade.
        let mut owners = owners.lock().unwrap_or_else(|e| e.into_inner());
        let me = std::thread::current().id();
        match owners.entry(path.clone()) {
            Entry::Occupied(e) if *e.get() != me => None,
            Entry::Occupied(_) => Some(path),
            Entry::Vacant(e) => {
                e.insert(me);
                Some(path)
            }
        }
    }

    /// Persist to a specific path, creating parent directories as needed.
    ///
    /// Returns whether bytes were actually written: `Ok(false)` means the
    /// file already held exactly this content and was left alone.
    ///
    /// #2286 review (non-blocking 1): every strip mutator persists, including
    /// gestures that change nothing (re-clicking the already-active tab), so
    /// without this the common case paid a `create_dir_all` + `fs::write` per
    /// click. The check lives HERE rather than at the call sites because only
    /// the file knows whether it is stale: `retain_known`'s load-time pruning
    /// mutates the in-memory set *before* the first mutator runs, so a
    /// "did this click change anything?" gate up in `CoordApp` would suppress
    /// exactly the write that drops a pruned document from the file (contract
    /// §6 bullet 2 — the sealed slice's
    /// `a_document_whose_issue_is_absent_from_the_board_is_pruned_on_load`
    /// asserts that re-save). Comparing against the file's real content is
    /// correct in both cases.
    pub(crate) fn save_to_path(&self, path: &std::path::Path) -> Result<bool, String> {
        // #2288: the persisted shape is per-scope (contract §6), so a split
        // scope has to be flattened onto one group on the way out. That
        // flattening UNIONS every pane rather than picking the focused one —
        // see `PaneSet::merged_for_persist` for why, and for what it keeps.
        // With one pane (every #2286 scenario) it is `focused().clone()`, so
        // this stays byte-identical to the pre-split writer.
        let persisted = PersistedTabs {
            board: Some(
                self.board
                    .merged_for_persist()
                    .to_persisted(PanelScope::Board),
            ),
            pipeline: Some(
                self.pipeline
                    .merged_for_persist()
                    .to_persisted(PanelScope::Pipeline),
            ),
        };
        let text =
            serde_json::to_string_pretty(&persisted).map_err(|e| format!("serialize tabs: {e}"))?;
        if std::fs::read_to_string(path).is_ok_and(|existing| existing == text) {
            return Ok(false);
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("create tabs dir: {e}"))?;
        }
        std::fs::write(path, text).map_err(|e| format!("write tabs: {e}"))?;
        Ok(true)
    }

    /// Persist to `~/.coord/tabs.json` unconditionally — creating the file
    /// if it doesn't exist yet. A no-op (`Ok`) when `HOME` is unset — the
    /// TUI stays functional without a home directory, it just won't
    /// remember open tabs across restarts.
    ///
    /// Called exactly once per run, on the way out (`CoordApp`'s `handle`
    /// wrapper in `render.rs`, on `Reaction::Exit`) — see [`Self::save_if_exists`]
    /// for the far more frequent mutation-triggered save, which deliberately
    /// does NOT create a fresh file.
    pub(crate) fn save(&self) -> Result<bool, String> {
        match &self.origin {
            Some(path) => self.save_to_path(path),
            None => Ok(false),
        }
    }

    /// Persist to `~/.coord/tabs.json`, but ONLY when that file already
    /// exists — never creates it. A no-op (`Ok`) when `HOME` is unset or the
    /// file isn't there yet.
    ///
    /// Called from every tab mutator (open/pin/activate/close/…, both
    /// scopes) so a restored session (or one that has already quit once,
    /// creating the file via [`Self::save`]) stays fresh in near-real-time
    /// rather than only on the next clean exit. Deliberately does NOT create
    /// a brand-new file: this crate's `#[cfg(test)]`/`test-support` fixture
    /// path (`fixtures.rs::make_test_app`) reads real `~/.coord/tabs.json`
    /// state at construction (contract §6 / #2286 needs this — see that
    /// function's doc comment), and the overwhelming majority of this
    /// crate's TuiDriver click-driven test suites (#2282-#2287) build their
    /// app this way WITHOUT sandboxing `HOME`. If every click that opens or
    /// activates a tab could CREATE `~/.coord/tabs.json` on whatever machine
    /// is running the tests, those tests would start writing into the real
    /// developer's home directory and — worse — polluting every other
    /// unsandboxed test that runs in the same process afterward, since
    /// `DocTabs::load()` would then pick up whatever a completely unrelated
    /// test just wrote. Gating on "already exists" closes that hole while
    /// still satisfying contract §6 exactly: a session that has quit once
    /// (creating the file) keeps it live-updated afterward, and the sealed
    /// acceptance slice's own pruning test (`tabs_persistence_2286::
    /// a_document_whose_issue_is_absent_from_the_board_is_pruned_on_load`)
    /// seeds the file BEFORE the mutation it checks, so this still fires for
    /// it.
    pub(crate) fn save_if_exists(&self) -> Result<bool, String> {
        let Some(path) = self.origin.as_ref() else {
            return Ok(false);
        };
        if !path.exists() {
            return Ok(false);
        }
        self.save_to_path(path)
    }
}

// ─── §6's pinned file shape ──────────────────────────────────────────────

/// One document key as written to `~/.coord/tabs.json` — `{"repo": …,
/// "issue": …}`, contract §6's pinned shape, verbatim. Also carries the
/// owning scope's "cheap" per-tab sub-state (§6's last bullet: the sub-tab
/// selection, persisted "as a string"; scroll offsets are deliberately NOT
/// here — the issue explicitly allows dropping them on restart, so they
/// simply reset to defaults on load) under the field name matching the
/// document's `active`/`preview` role: the tabs-list entry itself.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
struct PersistedDoc {
    repo: String,
    issue: u64,
    /// The sub-tab this document was on — `"board"`/`"issue"`/`"chat"`/
    /// `"terminal"` for a Board-scope document, `"overview"`/`"issue"`/
    /// `"log"`/`"summary"`/`"terminal"`/`"completed"` for a Pipeline-scope
    /// one. Absent on an older file, or when the document was never
    /// switched away from (`DetailSubState` is sparse, contract §5) — reads
    /// back as the scope's default sub-tab either way. An unrecognised
    /// string (future variant, hand-edited file) is likewise read as the
    /// default rather than rejecting the whole document.
    #[serde(skip_serializing_if = "Option::is_none")]
    sub_tab: Option<String>,
}

impl PersistedDoc {
    fn key(&self) -> DocKey {
        (self.repo.clone(), self.issue)
    }

    /// This document's restored sub-state, under `owner`'s scope — `None`
    /// when `sub_tab` is absent or unrecognised, which callers read as "no
    /// entry", exactly like a document that was never checkpointed.
    fn sub_state(&self, owner: PanelScope) -> Option<DetailSubState> {
        let s = self.sub_tab.as_deref()?;
        match owner {
            PanelScope::Board => board_tab_from_str(s).map(|board_tab| DetailSubState {
                board_tab,
                ..DetailSubState::default()
            }),
            PanelScope::Pipeline => pipeline_tab_from_str(s).map(|pipeline_tab| DetailSubState {
                pipeline_tab,
                ..DetailSubState::default()
            }),
        }
    }

    /// Build the persisted form of `key`, pulling `owner`'s sub-tab out of
    /// `state` (`None` for `active`/`preview`'s call sites, which pass
    /// `self.sub_state.get(key)` same as a tabs-list entry would — the
    /// document's sub-tab is a property of the document, not of which role
    /// it plays in the scope).
    fn from_key(key: &DocKey, state: Option<&DetailSubState>, owner: PanelScope) -> Self {
        let sub_tab = state.map(|s| match owner {
            PanelScope::Board => board_tab_to_str(s.board_tab).to_string(),
            PanelScope::Pipeline => pipeline_tab_to_str(s.pipeline_tab).to_string(),
        });
        PersistedDoc {
            repo: key.0.clone(),
            issue: key.1,
            sub_tab,
        }
    }
}

fn board_tab_to_str(tab: BoardDetailTab) -> &'static str {
    match tab {
        BoardDetailTab::Board => "board",
        BoardDetailTab::Issue => "issue",
        BoardDetailTab::Chat => "chat",
        BoardDetailTab::Terminal => "terminal",
    }
}

fn board_tab_from_str(s: &str) -> Option<BoardDetailTab> {
    match s {
        "board" => Some(BoardDetailTab::Board),
        "issue" => Some(BoardDetailTab::Issue),
        "chat" => Some(BoardDetailTab::Chat),
        "terminal" => Some(BoardDetailTab::Terminal),
        _ => None,
    }
}

fn pipeline_tab_to_str(tab: PipelineDetailTab) -> &'static str {
    match tab {
        PipelineDetailTab::Overview => "overview",
        PipelineDetailTab::Issue => "issue",
        PipelineDetailTab::Log => "log",
        PipelineDetailTab::Summary => "summary",
        PipelineDetailTab::Terminal => "terminal",
        PipelineDetailTab::Completed => "completed",
    }
}

fn pipeline_tab_from_str(s: &str) -> Option<PipelineDetailTab> {
    match s {
        "overview" => Some(PipelineDetailTab::Overview),
        "issue" => Some(PipelineDetailTab::Issue),
        "log" => Some(PipelineDetailTab::Log),
        "summary" => Some(PipelineDetailTab::Summary),
        "terminal" => Some(PipelineDetailTab::Terminal),
        "completed" => Some(PipelineDetailTab::Completed),
        _ => None,
    }
}

/// One scope's persisted object: ordered `tabs`, `active`, `preview`
/// (contract §6's pinned shape).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct PersistedScope {
    tabs: Vec<PersistedDoc>,
    active: Option<PersistedDoc>,
    preview: Option<PersistedDoc>,
}

/// The whole `~/.coord/tabs.json` (contract §6). Top-level keys are the
/// lowercase `PanelScope` names in use today — `serde`'s default
/// `snake_case`-of-the-Rust-field-name behaviour already produces exactly
/// `"board"`/`"pipeline"` from these field names, so no `#[serde(rename)]`
/// is needed. A scope absent from the file starts with no tabs.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct PersistedTabs {
    board: Option<PersistedScope>,
    pipeline: Option<PersistedScope>,
}

/// #2286 (ms-65 §6): every document key currently known to the loaded board —
/// the set [`DocTabs::retain_known`] prunes against, both on load and after
/// every real data refresh ("drop any document whose issue is absent from
/// the board"). Sourced from `open_issues` only, mirroring
/// `board_data_from_payload`'s `"issues"` → `open_issues` mapping every
/// fixture in this suite seeds through.
///
/// # #2481: an issue is known under BOTH of its repo spellings
///
/// `DocKey`'s repo half is a bare `String`, and the two panels that mint doc
/// keys disagree about which string it is:
///
/// * **Board** keys come from `board_selected_issue` → `board_active_repo`,
///   i.e. the **coord-local** repo name (`open_issues[*].repo_name`,
///   `claude-coordinator`).
/// * **Pipeline** keys come from `PipelineIssue::repo_slug` at every call
///   site (`events.rs`'s `RowSelected` / `RowActivated` arms), i.e. the
///   **GitHub slug** (`JDonaghy/claude-coordinator`) — and
///   `pipeline_issue_for` / `reveal_pipeline_active_doc` / the strip's own
///   label lookup all key off `repo_slug` to match.
///
/// [`DocTabs::retain_known`] is deliberately scope-agnostic (#2288: "every
/// pane of every scope"), so ONE set has to satisfy both spellings. Emitting
/// only `repo_name` meant every live-opened Pipeline tab failed the match and
/// was pruned by the very next `sync_doc_tabs()` tick — a few seconds after
/// it was clicked — and the removal was persisted straight to
/// `~/.coord/tabs.json`, so it did not come back on restart either (#2481).
///
/// Every pre-#2481 doc-tab fixture seeded no `board_meta`, leaving
/// `pipeline_repos` empty, so `repo_slug` fell back to `repo_name`
/// (`pipeline_issues_from_cache`) and the two spellings were accidentally the
/// same string — which is why no test caught this.
///
/// So each open issue contributes its local-name key AND, when
/// `pipeline_repos` maps that repo to a different slug, the slug-spelled key.
/// This widens the known set by an alias of an issue that IS on the board; it
/// does **not** weaken pruning, because an issue absent from `open_issues`
/// contributes neither spelling and is still dropped (§6 bullet 2 / #2286
/// AC 3 — see `a_pipeline_doc_tab_whose_issue_left_the_board_is_still_pruned`
/// and the sealed
/// `a_document_whose_issue_is_absent_from_the_board_is_pruned_on_load`).
///
/// Both spellings are emitted rather than normalising to one because
/// `tabs.json` already holds files written under the *local* name for the
/// `pipeline` scope (that is the shape the sealed §6 restore fixture pins),
/// while a freshly clicked Pipeline tab is slug-keyed — a restore must keep
/// honouring both.
///
/// This covers the Pipeline panel completely, not just the common case:
/// `pipeline_issues_from_cache` only ever emits a `PipelineIssue` it found in
/// `open_issues` — including the untracked-epic-child backfill, whose
/// "aged out of the `open_issues` cache (#771)" arm `continue`s rather than
/// pushing a row. So every key the Pipeline click path can mint
/// (`pipeline_issues[idx].repo_slug`) belongs to an issue that IS in
/// `open_issues`, and is therefore in this set under its slug spelling.
///
/// # Known remaining gap (Board scope) — out of scope for #2481
///
/// The Board sidebar is NOT sourced from `open_issues` alone:
/// `issues_by_repo` also synthesises an `IssueGroup` per `(repo,
/// issue_number)` found in `data.assignments`, so an issue dispatched via
/// `coord assign` before its repo's first `coord sync` (or one that has aged
/// out of the cache) still gets a Board row — the same "assignment-only Board
/// entries" case `apply_pending_data`'s terminal-session pruning already
/// calls out by name. A Board document tab opened on such a row is still
/// pruned by the next tick, because that issue is in neither spelling here.
/// #2481 explicitly scopes itself to Pipeline and asks for a follow-up rather
/// than a fix; sourcing the Board half from `board_issues_cache` (which is
/// exactly the set of issues the Board can show) is the shape that would
/// close it.
pub(crate) fn known_doc_keys(data: &BoardData) -> HashSet<DocKey> {
    let slug_of: std::collections::HashMap<&str, &str> = data
        .pipeline_repos
        .iter()
        .map(|(local, slug)| (local.as_str(), slug.as_str()))
        .collect();
    let mut out: HashSet<DocKey> = HashSet::new();
    for oi in &data.open_issues {
        if let Some(slug) = slug_of.get(oi.repo_name.as_str()) {
            if *slug != oi.repo_name.as_str() {
                out.insert(((*slug).to_string(), oi.number));
            }
        }
        out.insert((oi.repo_name.clone(), oi.number));
    }
    out
}

/// Build one document tab's rendered label (contract §2b/§2c/§2d/§1).
///
/// Shape, outermost first:
///
/// ```text
/// active   [∘ CW#101 Fix login race… ×]␠
/// inactive  ∘ CW#101 Fix login race… ×␠
///           ^ ^^                     ^
///           | |└─ `#<N> <title>` truncated to `max_cols` columns
///           | └─ #2641: repo alias (repo_alias, `owner/` stripped first),
///           |    joined directly to `#N` with no space — only when the
///           |    open set spans >1 repo
///           └─ §1 preview marker, only on the preview tab
///
/// (`×` at the end is the §2d close glyph, TAB_CLOSE_CHAR.)
/// ```
///
/// `max_cols` is [`DOC_TAB_LABEL_COLS`] (20, §2b) for an undivided strip
/// and [`SPLIT_DOC_TAB_LABEL_COLS`] (14, §9) for a strip inside a split
/// pane — two independently-pinned contract constants, which is why the
/// budget is a parameter rather than read from one of them here. The `∘ `
/// preview marker pushes the *rendered* width out by its own 2 columns in
/// both cases (22 / 16) rather than eating into the budget.
///
/// The trailing space is the inter-tab separator: quadraui's TUI tab-bar
/// rasteriser paints labels back-to-back with no gap of its own, so without it
/// `#101 …×#102 …` would run together. It sits *outside* the §2c brackets,
/// which wrap the tab's own content only.
///
/// The whole tab — close glyph included — lives in `TabItem::label` rather than
/// being assembled from `TabBar::show_tab_close`, because the rasteriser paints
/// the close glyph *after* the label and follows it with a separator space:
/// there is no way to get the §2c closing `]` to land to the right of `×` via
/// that path. `is_preview` is still set on the `TabItem` so the italic styling
/// the contract asks for is real; the `∘ ` marker is the symbols-only stand-in
/// for it, not a substitute (see [`PREVIEW_MARKER`]).
pub(crate) fn doc_tab_label(
    repo: &str,
    number: u64,
    title: &str,
    show_repo: bool,
    is_preview: bool,
    is_active: bool,
    max_cols: usize,
) -> String {
    let base = truncate_with_ellipsis(&format!("#{number} {title}"), max_cols);
    let mut inner = String::new();
    if is_preview {
        inner.push_str(PREVIEW_MARKER);
    }
    if show_repo {
        // #2641: the Pipeline strip's tab keys are the `pipeline_repos` slug
        // (`JDonaghy/coord-web`), while the Board strip's are the local repo
        // name (`coord-web`) — strip any `owner/` prefix before aliasing so
        // both land on the same two-letter alias the Queue grid uses
        // (`coord-web` → `CW`), joined directly to `#N` with no space.
        let basename = repo.rsplit('/').next().unwrap_or(repo);
        inner.push_str(&repo_alias(basename));
    }
    inner.push_str(&base);
    inner.push(' ');
    inner.push(quadraui::tui::TAB_CLOSE_CHAR);
    if is_active {
        format!("[{inner}] ")
    } else {
        format!("{inner} ")
    }
}

/// Character offset of the §2d close glyph within a rendered tab label, or
/// `None` if the label carries none (shouldn't happen — every tab built by
/// [`doc_tab_label`] appends exactly one, per
/// `every_label_carries_the_close_glyph` below). Used by the click
/// hit-test ([`resolve_doc_tab_click`]) to tell a click on the `×` from a
/// click on the rest of the tab.
///
/// The **last** occurrence is the close glyph, never the first:
/// [`doc_tab_label`] embeds the issue title verbatim (modulo truncation), so
/// a title like "Fix 2×2 grid" puts a `×` in the label's *body*. The glyph
/// [`doc_tab_label`] appends is always to the right of every title char
/// (only `]` and the separator space follow it), so scanning from the end
/// finds it unambiguously.
pub(crate) fn doc_tab_close_col(label: &str) -> Option<usize> {
    let total = label.chars().count();
    label
        .chars()
        .rev()
        .position(|c| c == quadraui::tui::TAB_CLOSE_CHAR)
        .map(|from_end| total - 1 - from_end)
}

/// Which part of a tab a resolved click landed on — contract §4's
/// "clicking a tab's `×` closes it; clicking its body activates it", plus
/// §4 (#2642)'s "clicking a baked `‹`/`›` overflow marker opens the
/// open-tabs picker instead of activating the tab it's painted into".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TabClickKind {
    /// Click landed on the tab's `×` close glyph. Index is into the strip.
    Close(usize),
    /// Click landed anywhere else on the tab. Index is into the strip.
    Body(usize),
    /// Click landed on a `‹`/`›` overflow marker (#2283) baked into the
    /// boundary tab's label by [`crate::app::CoordApp::board_doc_tab_strip`]
    /// / `pipeline_doc_tab_strip`. Index is the strip index of the tab the
    /// marker is baked into — irrelevant to callers, which route this
    /// straight to opening the #2642 quick-pick regardless of which tab it
    /// landed on.
    Overflow(usize),
}

/// Resolve a click at `click_x` (same coordinate space as `origin_x`)
/// against a rendered doc-tab strip's labels, honouring `scroll_offset`
/// exactly the way `hit_tab_index_from_labels` (dialogs.rs) does — tabs
/// before `scroll_offset` are skipped, and labels are walked left-to-right
/// from `origin_x` accumulating `chars().count()` widths, matching what the
/// TUI rasteriser actually paints (§0: every glyph this milestone
/// introduces, including `×`/`‹`/`›`, is one display column).
///
/// Deliberately reimplemented here rather than calling
/// `hit_tab_index_from_labels` and separately re-deriving each tab's start
/// column: the close-glyph offset needs the SAME cumulative-width walk that
/// function already does internally, and duplicating just the "where does
/// tab `idx` start" half without the shared loop would be the real second
/// algorithm the ms-65 design note warns against.
pub(crate) fn resolve_doc_tab_click(
    labels: &[&str],
    origin_x: f32,
    click_x: f32,
    scroll_offset: usize,
) -> Option<TabClickKind> {
    let mut cursor = origin_x;
    for (i, label) in labels.iter().enumerate().skip(scroll_offset) {
        let chars: Vec<char> = label.chars().collect();
        let width = chars.len() as f32;
        let end = cursor + width;
        if click_x >= cursor && click_x < end {
            let offset_in_tab = (click_x - cursor).floor() as usize;
            // #2642: a `‹`/`›` overflow marker is baked into column 0 or the
            // very last column of the boundary tab's label
            // (`bake_doc_tab_overflow_markers`, render.rs) — check those two
            // fixed positions before falling through to close/body so a
            // click there routes to the picker instead of activating (or, for
            // `‹`, closing — column 0 never carries the close glyph, but the
            // check still has to win the race) the tab it happens to be
            // painted into.
            if offset_in_tab == 0 && chars.first() == Some(&SCROLL_LEFT_MARKER) {
                return Some(TabClickKind::Overflow(i));
            }
            if !chars.is_empty()
                && offset_in_tab == chars.len() - 1
                && chars.last() == Some(&SCROLL_RIGHT_MARKER)
            {
                return Some(TabClickKind::Overflow(i));
            }
            return Some(match doc_tab_close_col(label) {
                Some(close_col) if close_col == offset_in_tab => TabClickKind::Close(i),
                _ => TabClickKind::Body(i),
            });
        }
        cursor = end;
    }
    None
}

// ── #2642 (ms-65 §… quick-pick): Ctrl+E open-tabs picker ─────────────────

/// One row of the Ctrl+E open-tabs picker — everything needed to render a
/// [`PaletteItem`] and, on activation, resolve back to a strip index for
/// `CoordApp::activate_board_doc_tab` / `activate_pipeline_doc_tab` (the
/// same entry points a click on the strip already uses, so preview/pin
/// semantics and the #2288 pane routing can't drift).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DocTabPickerRow {
    /// Index into the strip this row was built from — NOT the index into
    /// whatever filtered subset a picker is currently showing.
    pub(crate) strip_idx: usize,
    pub(crate) repo: String,
    pub(crate) number: u64,
    pub(crate) title: String,
    pub(crate) is_active: bool,
    pub(crate) is_preview: bool,
}

/// Contract: "the repo alias when the open set spans more than one repo" —
/// the exact same rule [`doc_tab_label`]'s callers apply to the strip
/// itself (`board_doc_tab_bar_for_pane` / `pipeline_doc_tab_bar`,
/// render.rs), so the picker's rows and the strip's tabs never disagree on
/// when a repo prefix is warranted.
pub(crate) fn doc_tab_picker_show_repo(rows: &[DocTabPickerRow]) -> bool {
    rows.first()
        .map(|first| rows.iter().any(|r| r.repo != first.repo))
        .unwrap_or(false)
}

/// Filter `rows` by `query` — contract: "Filtering matches on number and
/// title." Empty query returns every row, in strip order (the "unfiltered
/// initial list" convention every other picker in this file follows). A
/// non-empty query fuzzy-matches (same [`fuzzy_score`] the global issue
/// finder uses) against `"#<number> <title>"`, best score first, ties
/// broken by strip order so the result stays stable as the user keeps
/// typing.
pub(crate) fn filter_doc_tab_picker_rows(
    rows: Vec<DocTabPickerRow>,
    query: &str,
) -> Vec<DocTabPickerRow> {
    if query.is_empty() {
        return rows;
    }
    let mut scored: Vec<(u32, DocTabPickerRow)> = rows
        .into_iter()
        .filter_map(|row| {
            let haystack = format!("#{} {}", row.number, row.title);
            let (score, _) = fuzzy_score(query, &haystack)?;
            Some((score, row))
        })
        .collect();
    scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.strip_idx.cmp(&b.1.strip_idx)));
    scored.into_iter().map(|(_, row)| row).collect()
}

/// Render `rows` as `PaletteItem`s — contract: "Rows list the open tabs in
/// strip order: issue number, title, and the repo alias when the open set
/// spans more than one repo... Mark the active tab, and the preview tab the
/// way the strip does." Mirrors [`doc_tab_label`]'s `∘ ` preview marker and
/// `[...]` active brackets rather than re-deriving a second presentation
/// for the same two facts.
pub(crate) fn doc_tab_picker_items(rows: &[DocTabPickerRow], show_repo: bool) -> Vec<PaletteItem> {
    rows.iter()
        .map(|row| {
            let mut inner = String::new();
            if row.is_preview {
                inner.push_str(PREVIEW_MARKER);
            }
            if show_repo {
                let basename = row.repo.rsplit('/').next().unwrap_or(&row.repo);
                inner.push_str(&repo_alias(basename));
            }
            inner.push_str(&format!("#{} {}", row.number, row.title));
            let text = if row.is_active {
                format!("[{inner}]")
            } else {
                inner
            };
            PaletteItem {
                text: StyledText {
                    spans: vec![StyledSpan::plain(text)],
                },
                detail: None,
                icon: None,
                match_positions: Vec::new(),
                depth: 0,
                expandable: false,
                expanded: false,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u64) -> DocKey {
        ("claude-coordinator".to_string(), n)
    }

    // ── model: contract §2e ──────────────────────────────────────────────

    #[test]
    fn single_open_appends_one_preview_tab_and_activates_it() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(102));
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(0));
    }

    #[test]
    fn second_open_replaces_the_preview_in_place() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(102));
        g.open_preview(k(103));
        assert_eq!(g.tabs(), &[k(103)], "preview replaced, not appended");
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(0));
    }

    #[test]
    fn opening_an_already_open_document_only_activates_it() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        g.open_preview(k(101));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert_eq!(g.active_index(), Some(0));
        assert!(
            g.is_preview(1),
            "activating a pinned tab must not promote or evict the open preview"
        );
    }

    #[test]
    fn pin_drops_the_preview_slot() {
        let mut g = DocTabGroup::default();
        g.pin(k(102));
        assert_eq!(g.tabs(), &[k(102)]);
        assert!(!g.is_preview(0));
    }

    #[test]
    fn a_pinned_tab_plus_an_open_yields_two_tabs_one_preview() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(!g.is_preview(0));
        assert!(g.is_preview(1));
    }

    /// The sequence contract §2e explicitly warns must not be conflated with
    /// the two-tab intermediate state above.
    #[test]
    fn three_pins_yield_three_pinned_tabs_with_the_last_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert_eq!(g.tabs(), &[k(101), k(102), k(103)]);
        assert_eq!(g.active_index(), Some(2));
        assert!((0..3).all(|i| !g.is_preview(i)), "all three are pinned");
    }

    /// …and the branch that *does* extend it: a pin landing while a preview is
    /// open replaces that preview rather than appending (§2e's trace).
    #[test]
    fn pinning_while_a_preview_is_open_replaces_it_in_place() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        g.pin(k(103));
        assert_eq!(g.tabs(), &[k(101), k(103)], "#102 evicted, not kept");
        assert!(!g.is_preview(1));
    }

    #[test]
    fn never_more_than_one_preview_tab() {
        let mut g = DocTabGroup::default();
        for n in [101, 102, 103, 104, 105] {
            g.open_preview(k(n));
            assert_eq!(g.tabs().len(), 1, "preview slot is reused every time");
        }
    }

    #[test]
    fn activating_a_preview_tab_from_the_strip_promotes_it() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert!(g.is_preview(1));
        assert!(g.activate_index(1));
        assert!(!g.is_preview(1), "promote-on-select");
        assert_eq!(g.active_index(), Some(1));
    }

    #[test]
    fn activating_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.activate_index(7));
        assert_eq!(g.active_index(), Some(0));
    }

    // ── close: contract §4 (#2283) ───────────────────────────────────────

    #[test]
    fn closing_the_active_rightmost_tab_activates_its_left_neighbour() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert!(g.close(2), "#103 was active and rightmost");
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert_eq!(g.active_index(), Some(1), "left neighbour (#102) activates");
    }

    #[test]
    fn closing_the_active_leftmost_tab_activates_the_new_leftmost() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        g.activate_index(0); // #101 active, leftmost
        assert!(g.close(0));
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert_eq!(
            g.active_index(),
            Some(0),
            "no left neighbour — the new leftmost (#102) activates, not an \
             index-1 underflow wrap to the end"
        );
    }

    #[test]
    fn closing_an_inactive_tab_leaves_the_active_document_unchanged() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103)); // active, index 2
        assert!(g.close(0)); // close #101, an inactive left neighbour
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert_eq!(
            g.active_key(),
            Some(&k(103)),
            "the active DOCUMENT is unchanged — only its index re-based"
        );
        assert_eq!(g.active_index(), Some(1));
    }

    #[test]
    fn closing_the_last_open_tab_clears_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(g.close(0));
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn closing_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.close(7));
        assert_eq!(g.tabs(), &[k(101)]);
    }

    // ── #2287 (ms-65 §8c): tab context menu — close_others / close_all / promote ──

    #[test]
    fn close_others_leaves_only_the_target_tab_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert!(g.close_others(1));
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.active_index(), Some(0));
    }

    #[test]
    fn close_others_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        assert!(!g.close_others(9));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
    }

    #[test]
    fn close_others_preserves_the_survivors_preview_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert!(g.is_preview(1));
        assert!(g.close_others(1));
        assert_eq!(g.tabs(), &[k(102)]);
        assert!(g.is_preview(0), "the survivor was the preview tab");
    }

    #[test]
    fn close_all_empties_the_group() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        assert!(g.close_all());
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn close_all_on_an_empty_group_is_a_no_op() {
        let mut g = DocTabGroup::default();
        assert!(!g.close_all());
    }

    #[test]
    fn promote_drops_the_preview_marker_without_moving_active_or_order() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert_eq!(g.active_index(), Some(1), "single click activates #102");
        // Re-activate #101 so the promoted tab (idx 0) is NOT the active one —
        // "Pin tab" must target the clicked tab, not whichever is active.
        g.activate_index(0);
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(1));
        assert!(g.promote(1));
        assert!(!g.is_preview(1));
        assert_eq!(g.tabs(), &[k(101), k(102)], "order unchanged");
        assert_eq!(g.active_index(), Some(0), "active tab unchanged");
    }

    #[test]
    fn promote_on_an_already_pinned_tab_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.promote(0));
        assert_eq!(g.tabs(), &[k(101)]);
    }

    #[test]
    fn promote_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.promote(9));
    }

    #[test]
    fn closing_the_preview_tab_clears_the_preview_slot() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102)); // preview, index 1
        assert!(g.close(1));
        assert_eq!(g.tabs(), &[k(101)]);
        assert!(!g.is_preview(0));
    }

    #[test]
    fn closing_a_tab_left_of_the_preview_rebases_the_preview_index() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.open_preview(k(103)); // preview, index 2
        g.activate_index(0); // move active off the preview so close(0) hits an inactive tab
        assert!(g.close(0));
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert!(g.is_preview(1), "preview index re-based from 2 to 1");
    }

    // ── click resolution: contract §4 ─────────────────────────────────────

    #[test]
    fn doc_tab_close_col_finds_the_close_glyph() {
        let label = doc_tab_label("claude-coordinator", 101, "Fix login race timeout", false, false, false, DOC_TAB_LABEL_COLS);
        // "#101 Fix login race… × " — 20-column base + a space, so × sits at
        // char index 21.
        assert_eq!(doc_tab_close_col(&label), Some(21));
    }

    #[test]
    fn doc_tab_close_col_skips_a_close_char_inside_the_title() {
        // The title itself contains `×` ("2×2"), which lands in the rendered
        // label verbatim. The close glyph is the LAST occurrence — the one
        // doc_tab_label appends — never the title's.
        let label = doc_tab_label("claude-coordinator", 104, "Fix 2×2 grid layout", false, false, false, DOC_TAB_LABEL_COLS);
        let col = doc_tab_close_col(&label).expect("label carries a close glyph");
        let title_x = label
            .chars()
            .position(|c| c == quadraui::tui::TAB_CLOSE_CHAR)
            .unwrap();
        assert!(
            col > title_x,
            "close col {col} must be the trailing glyph, not the title's × at {title_x}: {label:?}"
        );
        // And it is genuinely the appended glyph: only the separator space
        // (and, on an active tab, `]`) may follow it.
        assert_eq!(
            label.chars().nth(col),
            Some(quadraui::tui::TAB_CLOSE_CHAR)
        );
        assert_eq!(label.chars().skip(col + 1).collect::<String>(), " ");
    }

    #[test]
    fn resolve_doc_tab_click_distinguishes_body_from_close() {
        // Two 4-char labels back-to-back: "ab×d" then "ef×h", starting at x=10.
        let labels = ["ab×d", "ef×h"];
        // Body click on the first tab.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 10.5, 0),
            Some(TabClickKind::Body(0))
        );
        // Close click on the first tab's × (offset 2 within the label).
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 12.5, 0),
            Some(TabClickKind::Close(0))
        );
        // Close click on the second tab's ×, at absolute column 16.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 16.5, 0),
            Some(TabClickKind::Close(1))
        );
        // Past the last tab.
        assert_eq!(resolve_doc_tab_click(&labels, 10.0, 18.5, 0), None);
    }

    #[test]
    fn resolve_doc_tab_click_honours_scroll_offset() {
        let labels = ["ab×d", "ef×h"];
        // With the first tab scrolled out, the second starts at origin_x.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 12.5, 1),
            Some(TabClickKind::Close(1))
        );
        // A click before origin_x (where the hidden tab would have been)
        // never resolves to the hidden tab.
        assert_eq!(resolve_doc_tab_click(&labels, 10.0, 8.0, 1), None);
    }

    #[test]
    fn resolve_doc_tab_click_routes_overflow_markers_to_picker() {
        // #2642: a left marker baked into column 0 of the FIRST visible
        // tab's label, and a right marker baked into the LAST column of
        // the LAST visible tab's label — exactly what
        // `bake_doc_tab_overflow_markers` (render.rs) produces.
        let labels = [format!("{SCROLL_LEFT_MARKER}ab×d"), format!("ef×h{SCROLL_RIGHT_MARKER}")];
        let refs: Vec<&str> = labels.iter().map(String::as_str).collect();
        // Column 0 of the first tab: the `‹` marker, not its body.
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 10.5, 0),
            Some(TabClickKind::Overflow(0))
        );
        // Body / close columns of the SAME (marker-carrying) label are
        // unaffected — everything just shifts one column right.
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 11.5, 0),
            Some(TabClickKind::Body(0))
        );
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 13.5, 0),
            Some(TabClickKind::Close(0))
        );
        // The last column of the second tab: the `›` marker.
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 19.5, 0),
            Some(TabClickKind::Overflow(1))
        );
        // Body / close columns of that label are unaffected.
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 15.5, 0),
            Some(TabClickKind::Body(1))
        );
        assert_eq!(
            resolve_doc_tab_click(&refs, 10.0, 17.5, 0),
            Some(TabClickKind::Close(1))
        );
    }

    // ── per-document sub-state: contract §5 (#2285) ──────────────────────

    fn issue_sub_state(scroll: usize) -> DetailSubState {
        DetailSubState {
            board_tab: BoardDetailTab::Issue,
            scroll,
            ..DetailSubState::default()
        }
    }

    #[test]
    fn sub_state_round_trips_for_an_open_document() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert_eq!(g.sub_state(&k(101)), None, "sparse until first written");
        g.set_sub_state(k(101), issue_sub_state(5));
        assert_eq!(g.sub_state(&k(101)), Some(&issue_sub_state(5)));
    }

    #[test]
    fn set_sub_state_ignores_a_document_that_is_not_open() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.set_sub_state(k(999), issue_sub_state(5));
        assert_eq!(
            g.sub_state(&k(999)),
            None,
            "a checkpoint racing a close must not resurrect discarded state"
        );
    }

    #[test]
    fn closing_a_tab_discards_its_sub_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.set_sub_state(k(101), issue_sub_state(5));
        assert!(g.close(0));
        g.pin(k(101));
        assert_eq!(
            g.sub_state(&k(101)),
            None,
            "§5: re-opening the same issue number starts from the defaults"
        );
    }

    #[test]
    fn closing_one_tab_leaves_the_other_tabs_sub_state_alone() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.set_sub_state(k(101), issue_sub_state(5));
        g.set_sub_state(k(102), issue_sub_state(9));
        assert!(g.close(1));
        assert_eq!(g.sub_state(&k(101)), Some(&issue_sub_state(5)));
        assert_eq!(g.sub_state(&k(102)), None);
    }

    /// Replace-in-place evicts a document just as surely as a close does, so
    /// its record has to go with it — otherwise re-opening that issue into a
    /// later preview slot would resume sub-state §5 says was discarded.
    #[test]
    fn evicting_the_preview_document_discards_its_sub_state() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(101));
        g.set_sub_state(k(101), issue_sub_state(5));
        g.open_preview(k(102)); // replaces #101 in place
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.sub_state(&k(101)), None);
        g.open_preview(k(101));
        assert_eq!(g.sub_state(&k(101)), None);
    }

    #[test]
    fn scopes_never_merge() {
        let mut t = DocTabs::default();
        t.group_mut(PanelScope::Board).pin(k(101));
        t.group_mut(PanelScope::Pipeline).pin(k(201));
        assert_eq!(t.group(PanelScope::Board).tabs(), &[k(101)]);
        assert_eq!(t.group(PanelScope::Pipeline).tabs(), &[k(201)]);
    }

    // ── labels: contract §2b/§2c/§2d/§1 ──────────────────────────────────

    #[test]
    fn truncate_with_ellipsis_keeps_short_strings_whole() {
        assert_eq!(truncate_with_ellipsis("#101 Fix", 20), "#101 Fix");
    }

    #[test]
    fn truncate_with_ellipsis_fits_the_ellipsis_inside_the_budget() {
        let out = truncate_with_ellipsis("#101 Fix login race timeout", 20);
        assert_eq!(out, "#101 Fix login race…");
        assert_eq!(out.chars().count(), 20);
    }

    #[test]
    fn truncate_with_ellipsis_zero_budget_is_empty() {
        assert_eq!(truncate_with_ellipsis("#101 Fix", 0), "");
    }

    /// Exact strings lifted from the ms-65 §2 mocks.
    #[test]
    fn pinned_inactive_label_matches_the_mock() {
        assert_eq!(
            doc_tab_label("claude-coordinator", 101, "Fix login race timeout", false, false, false, DOC_TAB_LABEL_COLS),
            "#101 Fix login race… × "
        );
    }

    #[test]
    fn pinned_active_label_is_bracketed() {
        assert_eq!(
            doc_tab_label("claude-coordinator", 103, "Race condition in poller", false, false, true, DOC_TAB_LABEL_COLS),
            "[#103 Race condition… ×] "
        );
    }

    #[test]
    fn preview_label_carries_the_marker_outside_the_20_column_budget() {
        let label =
            doc_tab_label("claude-coordinator", 102, "Auth token refresh bug", false, true, true, DOC_TAB_LABEL_COLS);
        assert_eq!(label, "[∘ #102 Auth token ref… ×] ");
        assert!(label.contains("∘ #102 Auth token ref… ×"));
    }

    #[test]
    fn multi_repo_labels_carry_the_repo_prefix() {
        // #2641: the prefix is the two-letter repo alias joined directly to
        // `#N`, not the full repo name with a separating space.
        let label = doc_tab_label("quadraui", 597, "Preview tier", true, false, false, DOC_TAB_LABEL_COLS);
        assert!(
            label.starts_with("Q#597 Preview tier"),
            "got {label:?}"
        );
    }

    #[test]
    fn multi_repo_label_strips_an_owner_slug_prefix_before_aliasing() {
        // #2641: the Pipeline strip's tab keys are `pipeline_repos` slugs
        // (`JDonaghy/coord-web`) — aliasing the slug naively would give
        // `JCW`-ish garbage, so the `owner/` part must be stripped first.
        let label = doc_tab_label(
            "JDonaghy/coord-web",
            26,
            "Fix the thing",
            true,
            false,
            false,
            DOC_TAB_LABEL_COLS,
        );
        assert!(label.starts_with("CW#26 Fix the thing"), "got {label:?}");
        assert!(
            !label.contains("JDonaghy") && !label.contains("coord-web"),
            "got {label:?}"
        );
    }

    #[test]
    fn every_label_carries_the_close_glyph() {
        for active in [false, true] {
            for preview in [false, true] {
                let label = doc_tab_label("r", 1, "t", false, preview, active, DOC_TAB_LABEL_COLS);
                assert_eq!(
                    label.matches(quadraui::tui::TAB_CLOSE_CHAR).count(),
                    1,
                    "one close glyph per tab (active={active}, preview={preview})"
                );
            }
        }
    }

    // ── #2286 (ms-65 §6): persistence — retain_known / save / load ────────

    #[test]
    fn retain_known_drops_a_dead_tab_and_leaves_the_rest_alone() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103)); // active
        let known: HashSet<DocKey> = [k(101), k(103)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.tabs(), &[k(101), k(103)]);
        assert_eq!(g.active_key(), Some(&k(103)), "active tab survived, untouched");
    }

    #[test]
    fn retain_known_activates_a_surviving_neighbour_when_active_is_pruned() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(199)); // active, about to be pruned
        let known: HashSet<DocKey> = [k(101), k(102)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(
            g.active_key() == Some(&k(101)) || g.active_key() == Some(&k(102)),
            "a surviving neighbour must become active, got {:?}",
            g.active_key()
        );
    }

    #[test]
    fn retain_known_empties_the_group_when_nothing_survives() {
        let mut g = DocTabGroup::default();
        g.pin(k(198));
        g.pin(k(199));
        g.retain_known(&HashSet::new());
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn retain_known_discards_a_dead_tabs_sub_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(199));
        g.set_sub_state(k(199), issue_sub_state(7));
        let known: HashSet<DocKey> = [k(101)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.sub_state(&k(199)), None);
    }

    fn tmp_json_path(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "coord_doc_tabs_test_{tag}_{}.json",
            std::process::id()
        ))
    }

    #[test]
    fn save_then_load_round_trips_three_pinned_board_tabs() {
        let path = tmp_json_path("save_board_pins");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.pin(k(102));
            g.pin(k(103));
        }
        tabs.save_to_path(&path).expect("save should succeed");
        let raw = std::fs::read_to_string(&path).unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        // Shape-level: `active`/`preview` are document keys or `null`.
        assert!(raw.contains("\"board\""));
        assert!(raw.contains("\"pipeline\""));

        let g = loaded.group(PanelScope::Board);
        assert_eq!(g.tabs(), &[k(101), k(102), k(103)]);
        assert_eq!(g.active_key(), Some(&k(103)));
        assert!(!g.is_preview(0) && !g.is_preview(1) && !g.is_preview(2));
    }

    #[test]
    fn save_then_load_round_trips_a_preview_tab_as_a_preview_tab() {
        let path = tmp_json_path("save_preview");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.open_preview(k(102)); // preview, active
        }
        tabs.save_to_path(&path).expect("save should succeed");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        let g = loaded.group(PanelScope::Board);
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(!g.is_preview(0));
        assert!(g.is_preview(1), "the preview tab must be restored AS a preview, not promoted");
        assert_eq!(g.active_key(), Some(&k(102)));
    }

    #[test]
    fn save_then_load_keeps_board_and_pipeline_scopes_separate() {
        let path = tmp_json_path("save_scopes");
        let mut tabs = DocTabs::default();
        tabs.group_mut(PanelScope::Board).pin(k(101));
        tabs.group_mut(PanelScope::Pipeline).pin(k(201));
        tabs.save_to_path(&path).expect("save");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(loaded.group(PanelScope::Board).tabs(), &[k(101)]);
        assert_eq!(loaded.group(PanelScope::Pipeline).tabs(), &[k(201)]);
    }

    #[test]
    fn load_from_path_missing_file_starts_with_no_tabs() {
        let path = tmp_json_path("missing_9999999");
        let _ = std::fs::remove_file(&path);
        let loaded = DocTabs::load_from_path(&path);
        assert!(loaded.group(PanelScope::Board).is_empty());
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_empty_file_starts_with_no_tabs() {
        let path = tmp_json_path("empty");
        std::fs::write(&path, b"").unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert!(loaded.group(PanelScope::Board).is_empty());
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_malformed_json_starts_with_no_tabs_never_a_partial_parse() {
        let path = tmp_json_path("malformed");
        std::fs::write(
            &path,
            b"{\"board\": {\"tabs\": [{\"repo\": \"r\", \"issue\": 101},",
        )
        .unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert!(
            loaded.group(PanelScope::Board).is_empty(),
            "a truncated file must not recover its well-formed prefix"
        );
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_prunes_a_document_whose_issue_is_gone() {
        // #199 is not in `known` at restore time (simulated directly against
        // a persisted-shaped file, mirroring what `retain_known` does once
        // real board data is available).
        let path = tmp_json_path("prune_on_load");
        let raw = r#"{"board": {"tabs": [
            {"repo": "claude-coordinator", "issue": 101},
            {"repo": "claude-coordinator", "issue": 199},
            {"repo": "claude-coordinator", "issue": 102}
        ], "active": {"repo": "claude-coordinator", "issue": 101}, "preview": null}}"#;
        std::fs::write(&path, raw).unwrap();
        let mut loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        let known: HashSet<DocKey> = [k(101), k(102)].into_iter().collect();
        loaded.retain_known(&known);
        assert_eq!(loaded.group(PanelScope::Board).tabs(), &[k(101), k(102)]);
    }

    /// #2286 review (non-blocking 1): saving the same value twice writes once.
    #[test]
    fn save_to_path_skips_the_write_when_the_file_already_matches() {
        let path = tmp_json_path("idempotent_save");
        let _ = std::fs::remove_file(&path);
        let mut tabs = DocTabs::default();
        tabs.group_mut(PanelScope::Board).pin(k(101));

        assert!(
            tabs.save_to_path(&path).unwrap(),
            "the first save creates the file"
        );
        assert!(
            !tabs.save_to_path(&path).unwrap(),
            "an unchanged value must not re-write the file"
        );

        // …but a real change still lands, including one that only moves a
        // document's sub-tab (which is all the exit hook usually changes).
        tabs.group_mut(PanelScope::Board).set_sub_state(
            k(101),
            DetailSubState {
                board_tab: BoardDetailTab::Chat,
                ..DetailSubState::default()
            },
        );
        assert!(
            tabs.save_to_path(&path).unwrap(),
            "a changed sub-tab must reach the file"
        );

        let reloaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(
            reloaded
                .group(PanelScope::Board)
                .sub_state(&k(101))
                .map(|s| s.board_tab),
            Some(BoardDetailTab::Chat)
        );
    }

    /// A stale file must still be reconciled even when the in-memory value
    /// did not change during this mutation — the load-time prune already
    /// changed it, before any mutator ran. Contract §6 bullet 2, and the
    /// exact case an in-memory "did this click change anything?" gate got
    /// wrong.
    #[test]
    fn save_to_path_rewrites_a_file_that_disagrees_even_after_a_no_op_mutation() {
        let path = tmp_json_path("stale_file_rewrite");
        let raw = r#"{"board": {"tabs": [
            {"repo": "claude-coordinator", "issue": 101},
            {"repo": "claude-coordinator", "issue": 199}
        ], "active": {"repo": "claude-coordinator", "issue": 101}, "preview": null}}"#;
        std::fs::write(&path, raw).unwrap();

        let mut loaded = DocTabs::load_from_path(&path);
        let known: HashSet<DocKey> = [k(101)].into_iter().collect();
        loaded.retain_known(&known);

        assert!(
            loaded.save_to_path(&path).unwrap(),
            "the pruned value disagrees with the file, so it must be written"
        );

        let reloaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(
            reloaded.group(PanelScope::Board).tabs(),
            &[k(101)],
            "#2286 §6 bullet 2: a pruned document must never round-trip back \
             into the re-saved file"
        );
    }

    // ── #2288: the persisted file is PINNED at construction ───────────────
    //
    // `HOME` is process-global but the acceptance binary is not: while one
    // test points `HOME` at a sandbox, every other test's app used to
    // resolve that same sandbox on its next tab click and overwrite it.
    // These pin `save`/`save_if_exists` to `DocTabs::origin` instead.

    #[test]
    fn an_instance_with_no_pinned_origin_never_writes_through_the_ambient_home() {
        let path = tmp_json_path("origin_unpinned_never_writes");
        // A real, populated file on disk — `save_if_exists`'s "already
        // exists" precondition is satisfied, so only the missing origin can
        // hold the write back.
        let mut seed = DocTabs::default();
        seed.group_mut(PanelScope::Board).pin(k(101));
        seed.save_to_path(&path).expect("seed should be written");
        let before = std::fs::read_to_string(&path).unwrap();

        // Every explicit-path constructor yields an unpinned instance.
        let mut tabs = DocTabs::load_from_path(&path);
        assert_eq!(tabs.origin, None, "an explicit-path load pins nothing");
        tabs.group_mut(PanelScope::Board).pin(k(102));

        assert_eq!(
            tabs.save_if_exists(),
            Ok(false),
            "#2288: with no pinned origin the mutation-triggered save is a no-op"
        );
        assert_eq!(
            tabs.save(),
            Ok(false),
            "#2288: and so is the on-exit save — neither may fall back to `HOME`"
        );
        let after = std::fs::read_to_string(&path).unwrap();
        let _ = std::fs::remove_file(&path);
        assert_eq!(
            before, after,
            "#2288: the file on disk is untouched — this is the write that used \
             to land in whichever sandbox another thread had installed"
        );
    }

    #[test]
    fn a_pinned_origin_is_what_save_writes_to_and_survives_a_home_change() {
        let path = tmp_json_path("origin_pinned_writes");
        let _ = std::fs::remove_file(&path);

        let mut tabs = DocTabs {
            origin: Some(path.clone()),
            ..Default::default()
        };
        tabs.group_mut(PanelScope::Board).pin(k(101));

        assert_eq!(
            tabs.save_if_exists(),
            Ok(false),
            "never creates: the file does not exist yet"
        );
        assert!(!path.exists(), "save_if_exists must not create the file");

        assert_eq!(tabs.save(), Ok(true), "the on-exit save creates it");
        tabs.group_mut(PanelScope::Board).pin(k(102));
        assert_eq!(
            tabs.save_if_exists(),
            Ok(true),
            "and now that it exists, every mutation keeps it fresh"
        );

        let reloaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(
            reloaded.group(PanelScope::Board).tabs(),
            &[k(101), k(102)],
            "#2288: writes go to the pinned origin, not to a re-derived path"
        );
    }

    #[test]
    fn a_sandboxed_tabs_file_is_claimed_by_one_thread_and_refused_to_others() {
        let path = tmp_json_path("origin_claim");

        assert_eq!(
            DocTabs::claim_origin(path.clone()),
            Some(path.clone()),
            "#2288: the first thread to ask owns the file",
        );
        assert_eq!(
            DocTabs::claim_origin(path.clone()),
            Some(path.clone()),
            "#2288: and it keeps it across a restart within the same test",
        );

        let other = path.clone();
        let seen = std::thread::spawn(move || DocTabs::claim_origin(other))
            .join()
            .expect("claim thread should not panic");
        assert_eq!(
            seen, None,
            "#2288: a second thread is refused — it must not load the owner's \
             seeded tabs, and must never write over them",
        );

        // A different sandbox (every `HomeSandbox` mixes in a fresh sequence
        // number) is unaffected by the claim above.
        let fresh = tmp_json_path("origin_claim_other");
        assert_eq!(
            DocTabs::claim_origin(fresh.clone()),
            Some(fresh),
            "#2288: the claim is per-file, not a global latch",
        );
    }

    /// #2286 review (non-blocking 2): a hand-edited file that lists the same
    /// `{repo, issue}` twice must not produce two identical tabs. `index_of`
    /// can only ever resolve to the first, so the duplicate would render as a
    /// tab that cannot be activated and whose `×` closes the other one.
    #[test]
    fn load_from_path_dedupes_a_document_listed_twice() {
        let path = tmp_json_path("dupe_docs");
        let raw = r#"{"board": {"tabs": [
            {"repo": "claude-coordinator", "issue": 101},
            {"repo": "claude-coordinator", "issue": 102},
            {"repo": "claude-coordinator", "issue": 101}
        ], "active": {"repo": "claude-coordinator", "issue": 102}, "preview": null}}"#;
        std::fs::write(&path, raw).unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(
            loaded.group(PanelScope::Board).tabs(),
            &[k(101), k(102)],
            "the duplicate is dropped and the first occurrence keeps its slot"
        );
        assert_eq!(
            loaded.group(PanelScope::Board).active_index(),
            Some(1),
            "…and `active` still resolves to the document it named"
        );
    }

    /// The dedupe must keep the surviving tab's sub-state, and must not let a
    /// later duplicate's (absent or different) record win over the first's.
    #[test]
    fn load_from_path_dedupe_keeps_the_first_occurrences_sub_tab() {
        let path = tmp_json_path("dupe_sub_tab");
        let raw = r#"{"board": {"tabs": [
            {"repo": "claude-coordinator", "issue": 101, "sub_tab": "issue"},
            {"repo": "claude-coordinator", "issue": 101, "sub_tab": "chat"}
        ], "active": {"repo": "claude-coordinator", "issue": 101}, "preview": null}}"#;
        std::fs::write(&path, raw).unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(loaded.group(PanelScope::Board).tabs(), &[k(101)]);
        assert_eq!(
            loaded
                .group(PanelScope::Board)
                .sub_state(&k(101))
                .map(|s| s.board_tab),
            Some(BoardDetailTab::Issue),
            "first occurrence wins, matching what `index_of` would have picked"
        );
    }

    #[test]
    fn sub_tab_selection_round_trips_through_save_and_load() {
        let path = tmp_json_path("sub_tab");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.set_sub_state(
                k(101),
                DetailSubState {
                    board_tab: BoardDetailTab::Issue,
                    ..DetailSubState::default()
                },
            );
        }
        tabs.save_to_path(&path).expect("save");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(
            loaded.group(PanelScope::Board).sub_state(&k(101)),
            Some(&DetailSubState {
                board_tab: BoardDetailTab::Issue,
                ..DetailSubState::default()
            }),
        );
    }

    #[test]
    fn known_doc_keys_reads_repo_and_issue_from_open_issues() {
        use crate::app::types::OpenIssue;
        let data = BoardData {
            open_issues: vec![OpenIssue {
                repo_name: "claude-coordinator".to_string(),
                number: 101,
                title: "t".to_string(),
                body: String::new(),
                labels: Vec::new(),
                state: "open".to_string(),
                milestone_number: None,
                milestone_title: None,
                body_truncated: false,
                body_len: None,
                // #1941: fields added to the generated wire DTO; not exercised by this test.
                synced_at: None,
            }],
            ..BoardData::default()
        };
        let known = known_doc_keys(&data);
        assert!(known.contains(&k(101)));
        assert_eq!(known.len(), 1);
    }

    /// #2481, at the model level: with a `pipeline_repos` slug map the known
    /// set must contain the issue under BOTH spellings, because Board keys are
    /// local-name-spelled and Pipeline keys are slug-spelled — and one
    /// scope-agnostic `retain_known` has to accept both. See
    /// [`known_doc_keys`]'s own doc comment for the full derivation.
    fn slugged_board(numbers: &[u64]) -> BoardData {
        use crate::app::types::OpenIssue;
        BoardData {
            open_issues: numbers
                .iter()
                .map(|n| OpenIssue {
                    repo_name: "claude-coordinator".to_string(),
                    number: *n,
                    title: "t".to_string(),
                    body: String::new(),
                    labels: vec!["coord".to_string()],
                    state: "open".to_string(),
                    milestone_number: None,
                    milestone_title: None,
                    body_truncated: false,
                    body_len: None,
                    // #1941: fields added to the generated wire DTO; not exercised by this test.
                    synced_at: None,
                })
                .collect(),
            pipeline_repos: vec![(
                "claude-coordinator".to_string(),
                "JDonaghy/claude-coordinator".to_string(),
            )],
            ..BoardData::default()
        }
    }

    #[test]
    fn known_doc_keys_accepts_both_the_local_name_and_the_github_slug() {
        let known = known_doc_keys(&slugged_board(&[101]));
        assert!(
            known.contains(&("claude-coordinator".to_string(), 101)),
            "#2481: the Board's local-name spelling is still known"
        );
        assert!(
            known.contains(&("JDonaghy/claude-coordinator".to_string(), 101)),
            "#2481: …and so is the Pipeline's `repo_slug` spelling, which is \
             what `open_pipeline_doc_tab` is actually keyed by"
        );
        assert_eq!(known.len(), 2, "exactly the two spellings, nothing else");
    }

    /// The widening must not become "never prune anything": an issue that is
    /// on NO board is unknown under either spelling, which is what keeps §6
    /// bullet 2 (#2286 AC 3) intact.
    #[test]
    fn known_doc_keys_still_excludes_an_issue_that_is_not_on_the_board() {
        let known = known_doc_keys(&slugged_board(&[101]));
        assert!(!known.contains(&("claude-coordinator".to_string(), 199)));
        assert!(!known.contains(&("JDonaghy/claude-coordinator".to_string(), 199)));
    }

    /// A repo whose slug is not configured (or is literally its local name)
    /// contributes exactly one key — no degenerate duplicate, so the set size
    /// stays an honest count of distinct spellings.
    #[test]
    fn known_doc_keys_emits_one_key_when_the_slug_equals_the_local_name() {
        use crate::app::types::OpenIssue;
        let data = BoardData {
            open_issues: vec![OpenIssue {
                repo_name: "claude-coordinator".to_string(),
                number: 101,
                title: "t".to_string(),
                body: String::new(),
                labels: Vec::new(),
                state: "open".to_string(),
                milestone_number: None,
                milestone_title: None,
                body_truncated: false,
                body_len: None,
                // #1941: fields added to the generated wire DTO; not exercised by this test.
                synced_at: None,
            }],
            pipeline_repos: vec![(
                "claude-coordinator".to_string(),
                "claude-coordinator".to_string(),
            )],
            ..BoardData::default()
        };
        let known = known_doc_keys(&data);
        assert_eq!(known, HashSet::from([k(101)]));
    }

    // ── #2288 (ms-65 §9): the pane model ─────────────────────────────────

    #[test]
    fn a_fresh_scope_holds_exactly_one_focused_pane() {
        let panes = PaneSet::default();
        assert!(!panes.is_split());
        assert_eq!(panes.focused_index(), 0);
        assert!(panes.focused().is_empty());
    }

    /// The one thing #2288's ⚠ and contract Note 1 both say must not be
    /// guessed: quadraui's `Horizontal` is **side-by-side**, so the derived
    /// tree must lay the two panes out left/right at the same `y`.
    #[test]
    fn the_derived_tree_lays_the_panes_out_side_by_side() {
        use quadraui::event::Rect as QRect;
        use quadraui::primitives::split_tree::SplitTreeMeasure;

        let mut panes = PaneSet::default();
        assert!(panes.split_focused());
        let layout = panes
            .split_tree(PanelScope::Board)
            .layout(QRect::new(38.0, 1.0, 82.0, 38.0), SplitTreeMeasure::new(1.0));

        assert_eq!(layout.leaves.len(), 2);
        let (_, left) = layout.leaves[0];
        let (_, right) = layout.leaves[1];
        assert!(
            left.x < right.x,
            "first leaf is the LEFT pane (quadraui `Horizontal`)"
        );
        assert_eq!(
            left.y, right.y,
            "…and they share a row — a vimcode-style guess would stack them"
        );
        assert_eq!(left.height, right.height);
        let div = layout.dividers[0];
        assert_eq!(div.cell_position(), 78, "50/50 of 82 columns, minus the divider");
    }

    #[test]
    fn splitting_focuses_the_new_empty_pane_and_caps_at_two() {
        let mut panes = PaneSet::default();
        panes.focused_mut().pin(k(101));
        assert!(panes.split_focused());
        assert!(panes.is_split());
        assert_eq!(panes.focused_index(), 1, "the new pane takes focus");
        assert!(panes.focused().is_empty(), "…and starts empty");
        assert_eq!(panes.pane(0).tabs(), &[k(101)], "the original pane is untouched");
        assert!(
            !panes.split_focused(),
            "ms-65 ships side-by-side only — two panes maximum"
        );
    }

    #[test]
    fn focus_next_wraps_and_is_inert_at_one_pane() {
        let mut panes = PaneSet::default();
        assert!(!panes.focus_next(), "nothing to move to at one pane");
        panes.split_focused();
        assert_eq!(panes.focused_index(), 1);
        assert!(panes.focus_next());
        assert_eq!(panes.focused_index(), 0, "wraps");
        assert!(panes.focus_next());
        assert_eq!(panes.focused_index(), 1);
    }

    #[test]
    fn closing_the_last_pane_is_a_no_op() {
        let mut panes = PaneSet::default();
        panes.focused_mut().pin(k(101));
        assert!(!panes.close_focused_pane());
        assert_eq!(panes.focused().tabs(), &[k(101)], "nothing was destroyed");
    }

    #[test]
    fn closing_a_pane_leaves_the_survivors_tabs_alone() {
        let mut panes = PaneSet::default();
        panes.focused_mut().pin(k(101));
        panes.split_focused();
        panes.focused_mut().pin(k(102));
        assert!(panes.close_focused_pane());
        assert!(!panes.is_split());
        assert_eq!(panes.focused().tabs(), &[k(101)]);
    }

    #[test]
    fn emptying_a_pane_collapses_the_split() {
        let mut panes = PaneSet::default();
        panes.focused_mut().pin(k(101));
        panes.split_focused();
        panes.focused_mut().pin(k(102));
        // Close the focused pane's only tab, exactly as a `×` click would.
        assert!(panes.focused_mut().close(0));
        assert!(panes.collapse_empty_panes());
        assert!(!panes.is_split());
        assert_eq!(panes.focused().tabs(), &[k(101)]);
    }

    #[test]
    fn collapsing_two_empty_panes_still_leaves_one() {
        let mut panes = PaneSet::default();
        panes.split_focused();
        assert!(panes.collapse_empty_panes());
        assert_eq!(panes.focused_index(), 0);
        assert!(panes.focused().is_empty(), "a scope always has ≥1 pane");
    }

    /// The divider ratio is clamped by the primitive
    /// (`MIN_RATIO`..=`MAX_RATIO`), never by hand-rolled arithmetic here.
    #[test]
    fn the_split_ratio_is_clamped_by_the_primitive() {
        let mut panes = PaneSet::default();
        assert!(
            !panes.set_ratio(PanelScope::Board, 0.3),
            "an unsplit scope has no divider to move"
        );
        panes.split_focused();
        assert!(panes.set_ratio(PanelScope::Board, -5.0));
        assert_eq!(panes.ratio, quadraui::primitives::split_tree::MIN_RATIO);
        assert!(panes.set_ratio(PanelScope::Board, 5.0));
        assert_eq!(panes.ratio, quadraui::primitives::split_tree::MAX_RATIO);
    }

    /// `DocTabs::group` keeps reporting the FOCUSED pane, which is what
    /// leaves every #2282–#2287 call site untouched by the split.
    #[test]
    fn doc_tabs_group_follows_the_focused_pane() {
        let mut tabs = DocTabs::default();
        tabs.group_mut(PanelScope::Board).pin(k(101));
        assert_eq!(tabs.group(PanelScope::Board).tabs(), &[k(101)]);
        tabs.panes_mut(PanelScope::Board).split_focused();
        assert!(
            tabs.group(PanelScope::Board).is_empty(),
            "the new pane is focused, and it is empty"
        );
        tabs.group_mut(PanelScope::Board).pin(k(102));
        assert_eq!(tabs.panes(PanelScope::Board).pane(0).tabs(), &[k(101)]);
        assert_eq!(tabs.panes(PanelScope::Board).pane(1).tabs(), &[k(102)]);
    }

    /// #2286 (§6) is unaffected: pruning reaches every pane, not just the
    /// focused one.
    #[test]
    fn retain_known_prunes_background_panes_too() {
        let mut tabs = DocTabs::default();
        tabs.group_mut(PanelScope::Board).pin(k(101));
        tabs.panes_mut(PanelScope::Board).split_focused();
        tabs.group_mut(PanelScope::Board).pin(k(102));
        let known: HashSet<DocKey> = [k(102)].into_iter().collect();
        tabs.retain_known(&known);
        assert!(
            tabs.panes(PanelScope::Board).pane(0).is_empty(),
            "the background pane's unknown document is pruned too"
        );
        assert_eq!(tabs.panes(PanelScope::Board).pane(1).tabs(), &[k(102)]);
    }
}
