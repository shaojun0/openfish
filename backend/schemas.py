"""Pydantic request models, response models, and shared dataclasses.

The response models below are the single source of truth for `/openapi.json`.
They are also used to validate real responses in the end-to-end check, so a
view that changes shape fails that check instead of silently drifting away
from its published contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask_openapi3 import FileStorage
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FormatQuery(BaseModel):
    """Optional ``?format=json`` on the simple API."""

    format: str | None = Field(default=None, pattern=r"^(json)?$")


class SimpleProjectPath(BaseModel):
    """``<package_name>`` on the per-project simple API.

    flask-openapi3 binds path parameters through a model named ``path`` in the
    view signature (a plain ``str`` argument would be dropped before the view
    runs), so the path variable is declared here like any other request input.
    """

    package_name: str


class PyPIUploadForm(BaseModel):
    """``multipart/form-data`` body of ``POST /`` — a ``twine upload``.

    Two details make this a *file* binding rather than a text one, and getting
    either of them wrong is easy to miss:

    * the view must name the parameter ``form`` (flask-openapi3 looks the model up
      by that reserved name in the view's annotations), and the route must be
      registered by ``@pypi_bp.post`` rather than ``@pypi_bp.route`` — only the
      per-verb decorators run the request-binding wrapper;
    * the field must be annotated with ``flask_openapi3.FileStorage``, the
      subclass carrying the pydantic hooks that emit
      ``{"type": "string", "format": "binary"}``.  That schema is what tells the
      binder to read the part out of ``request.files``: a plain ``werkzeug``
      ``FileStorage`` has no such hooks and pydantic refuses to build the model
      at all, and ``FileStorage | None`` wraps the schema in an ``anyOf`` that the
      binder does not recognise — it looks among the form's *text* fields instead
      and rejects every upload as "content is required".

    Only ``content`` is declared, because it is the one part the server acts on.
    The rest of what twine sends (``:action``, ``name``, ``version``,
    ``filetype``, ``pyversion``, ``protocol_version``) is ignored as an unknown
    form key — exactly as it was before this model existed — and stays described
    on the endpoint itself in ``routes/pypi.py``.
    """

    content: FileStorage = Field(
        description="The distribution file (.whl, .tar.gz, .zip or .tar)"
    )


class NpmPublishDocument(BaseModel):
    """JSON body of ``PUT /npm/<package>`` — the document ``npm publish`` sends.

    This is what replaced ``request.get_json(silent=True, force=True)`` in
    ``routes/npm.py``: the body is now declared in the view signature like any
    other request input, and ``@validate_request()`` hands the view a parsed
    model instead of a raw dict.

    The model is deliberately *permissive*, because npm's publish document is an
    open-ended packument delta and the authority on what a publish may contain is
    ``services/npm_publish.py``.  That module re-derives every fact it stores
    from the uploaded bytes and refuses a malformed document with a ``400`` that
    names the offending field — a better answer than the binder's field-location
    message, which is why nothing here is ``required``: a document missing
    ``name`` or ``_attachments`` still reaches the service and is refused there
    in prose.  What the model does pin is that the body is a JSON *object*; a
    bare array, string or null never reaches the view.

    Three details are load-bearing:

    * ``_id`` and ``_attachments`` are pydantic *aliases* — a field name may not
      start with an underscore, but the wire keys must keep theirs.  The view
      therefore dumps with ``by_alias=True``, which is also what keeps the
      OpenAPI property names matching what a client actually sends;
    * ``extra="allow"`` carries every other packument key the CLI includes
      (``readme``, ``maintainers``, ``_npmUser``, …) through the dump instead of
      dropping it;
    * the *values* of ``versions`` and ``_attachments`` are declared only as far
      as the service already insists on them — an object — so nothing inside a
      manifest or an attachment is re-validated here, and a value the service
      tolerates (a non-string dist-tag, an attachment keyed oddly) is not
      rejected by the binder first.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str | None = Field(
        default=None,
        description="`left-pad`, or the scoped `@scope/name`; `_id` is the fallback",
    )
    id_: str | None = Field(default=None, alias="_id")
    description: str | None = Field(default=None, description="Package description")
    dist_tags: dict[str, Any] = Field(
        default_factory=dict,
        alias="dist-tags",
        description='Tags to set, e.g. `{"latest": "1.0.0"}`',
    )
    versions: dict[str, dict] = Field(
        default_factory=dict,
        description=(
            "Version manifests keyed by version; each `dist` states the "
            "shasum/integrity the uploaded bytes must hash to"
        ),
    )
    attachments: dict[str, dict] = Field(
        default_factory=dict,
        alias="_attachments",
        description=(
            "One entry per tarball being uploaded, keyed by its `.tgz` name "
            "(`content_type`, `length`, and the base64 `data`)"
        ),
    )
    access: str | None = Field(default=None, description="npm's public/restricted hint")


class UserListQuery(BaseModel):
    """``?limit=&offset=`` on ``GET /api/v1/admin/users``.

    Both bounds are left to the view, which *clamps* rather than rejects — so
    neither field carries a ``ge``/``le`` constraint here.  Declaring one would
    turn ``?limit=5000`` into a 400, and the route has always answered it with
    the last 1000 accounts.
    """

    limit: int | None = Field(default=None, description="Accounts to return (default 200)")
    offset: int | None = Field(default=None, description="Accounts to skip (default 0)")


class NpmSearchQuery(BaseModel):
    """``?text=&size=&from=`` on ``GET /npm/-/v1/search``.

    All three are ``str`` on purpose, and that is the whole design: this route
    has always *tolerated* a malformed ``from`` (it means 0) and *clamped* an
    out-of-range ``size``, so the model declares the input and the view keeps the
    decisions.  An ``int`` field — or a ``ge=1``/``le=`` bound — would answer
    ``?from=abc`` and ``?size=999`` with a 400, which is exactly the tolerance
    ``services.npm_registry.clamp_search_size`` and the ``check_npm_proxy`` gate
    exist to preserve.
    """

    model_config = ConfigDict(populate_by_name=True)

    text: str | None = Field(default=None, description="Search terms; empty means an empty result set")
    size: str | None = Field(default=None, description="Results to return, clamped to 1..250 (default 20)")
    from_: str | None = Field(default=None, alias="from", description="Offset; negatives are clamped to 0")


class ToolUploadForm(BaseModel):
    """``multipart/form-data`` body of ``POST /api/v1/tools``.

    ``file`` must be annotated with ``flask_openapi3.FileStorage`` and must not
    be optional: that schema (``{"type": "string", "format": "binary"}``) is what
    sends the binder looking in ``request.files`` at all, and wrapping it in
    ``FileStorage | None`` makes it look among the form's *text* fields instead —
    the same trap ``PyPIUploadForm`` documents at length.

    ``category`` is optional and, like the file part's own filename, is validated
    by ``services.hub_upload`` rather than here: an empty value means the tools
    root.
    """

    file: FileStorage = Field(description="The tool to store, as one file part")
    category: str = Field(default="", description="One sub-directory of TOOLS_DIR; empty means the root")


class ModelRouteRequest(BaseModel):
    """JSON body of the model-route writes (``POST``/``PUT /api/v1/models``).

    Permissive on purpose — ``services.model_routes`` owns every rule, and it
    tolerates more than these annotations suggest: ``provider``/``kind`` are
    matched case-insensitively through an alias table, ``aliases`` may arrive as
    a list *or* a comma-separated string, and ``enabled`` accepts
    ``"false"``/``"0"``/``"no"``/``"off"`` as well as a boolean.  The types here
    describe the JSON a client should send; the service is what decides.

    Two properties of this model are load-bearing for ``PUT``:

    * ``provider``/``kind`` are plain strings, never an ``enum``/``Literal`` —
      an enum would reject `OpenAI` and the provider aliases the service accepts;
    * nothing is required and the view dumps with ``exclude_unset=True``, because
      the service reads fields with ``payload.get(field, existing[field])``: an
      omitted field means "keep the stored value" while an explicit ``null``
      means "default".  Dumping every field would erase that distinction.

    ``api_key`` keeps its three-state meaning — omitted/null keeps the stored
    (sealed) value, ``""`` clears it, anything else is validated and sealed.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str | None = Field(
        default=None,
        description="Unique route name; required on create, renames the route when changed",
    )
    provider: str | None = Field(
        default=None,
        description=(
            "Wire format (`openai` unless the route says otherwise). The accepted "
            "set lives in `services.model_routes.PROVIDERS`; matching is "
            "case-insensitive and aliases are accepted, so this field is a plain "
            "string rather than an enum."
        ),
    )
    kind: str | None = Field(
        default=None,
        description=(
            "Model function — chat / completion / embedding / rerank / ocr / asr / "
            "tts. Omitted or empty means the protocol default (`ocr` for `mineru`)."
        ),
    )
    base_url: str | None = Field(default=None, description="Required http(s) URL of the upstream")
    api_key: str | None = Field(
        default=None,
        description=(
            "Upstream key. Sealed with MODEL_ROUTE_KEY before storage, so the table "
            "never holds a plaintext credential. Omitted or null keeps the stored "
            "key; empty string clears it. Supplying one on a deployment with no "
            "MODEL_ROUTE_KEY is refused rather than stored in the clear."
        ),
    )
    model: str | None = Field(default=None, description="Upstream model id, e.g. `gpt-4o-mini`")
    aliases: list[str] | str | None = Field(
        default=None,
        description=(
            "Names a downstream deployment may address this route by, including "
            "`default`. A comma-separated string is accepted too."
        ),
    )
    path: str | None = Field(default=None, description="Request path; defaults by protocol")
    enabled: bool | None = Field(default=None, description="Disabled routes stay listed but are not served")
    description: str | None = Field(default=None, description="Required on create; shown in the console")


class ModelRouteProbeRequest(BaseModel):
    """JSON body of ``POST /api/v1/models/probe`` — an unsaved draft.

    Same permissiveness as :class:`ModelRouteRequest`, minus the stored route:
    ``base_url`` is the only field the probe cannot work without, and its
    ``api_key`` arrives as plaintext (nothing was ever stored to keep).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    provider: str | None = Field(default=None, description="Wire format; defaults to `openai`")
    base_url: str | None = Field(default=None, description="Required http(s) URL to probe")
    api_key: str | None = Field(default=None, description="Plaintext key to try; validated as a header value")
    path: str | None = Field(default=None, description="Request path; defaults by protocol")


class DockerTagsQuery(BaseModel):
    """``?n=&last=`` on ``GET /docker/v2/<name>/tags/list`` — OCI pagination.

    Both fields are declared as plain strings, because neither has ever
    rejected anything: the view read them with ``request.args.get("last", "")``
    and ``request.args.get("n", type=int)``, and ``type=int`` answers an
    unparseable ``n`` with ``None`` — "no limit" — rather than an error.  A
    typed ``int`` would turn ``?n=abc`` (and an empty ``?n=``) into the binding
    envelope's ``400``, and a ``ge=0`` would do the same to a negative ``n``
    that the route has always clamped.

    So the conversion and the bound stay in ``routes/docker.py::_tag_limit``,
    where the ``len(tags) > limit`` clamp already lives.  The OCI
    ``minimum: 0`` is still published — from the endpoint's own
    ``parameters=[…]`` in ``routes/docker.py``, which is what the OpenAPI
    document is built from.
    """

    n: str | None = Field(default=None, description="Maximum number of tags to return.")
    last: str | None = Field(default=None, description="Return tags lexically after this one.")


class DockerUploadForm(BaseModel):
    """``multipart/form-data`` body of ``POST /api/v1/docker``.

    ``file`` must be annotated with ``flask_openapi3.FileStorage`` and must not
    be optional: that schema (``{"type": "string", "format": "binary"}``) is what
    sends the binder looking in ``request.files`` at all, while
    ``FileStorage | None`` wraps it in an ``anyOf`` the binder does not
    recognise and makes it look among the form's *text* fields instead — the
    trap ``PyPIUploadForm`` documents at length.

    Which names may be stored is a rule of ``services.hub_upload.docker_target``
    (one path segment, an allowed suffix, a name the catalog will list), not of
    this model: the model's only job is to carry the part.  Whether the part has
    a usable *filename* is checked in the view, because a part with an empty one
    still parses as a file and the binding cannot see the difference.
    """

    file: FileStorage = Field(
        description=(
            "The artifact to store: a `docker save` image tarball (.tar, "
            ".tar.gz, .tgz), a compose file (.yml, .yaml) or a Dockerfile"
        )
    )


class DebianSnapshotQuery(BaseModel):
    """``?suites=&components=&arches=&fresh=`` on ``GET /debian/offline/snapshot``.

    The first three narrow the walk and are parsed by
    ``services.debian_offline.split_list``, which takes the raw value — so they
    stay strings here.  ``fresh`` is a string for a stronger reason: the view has
    always read it through ``_truthy``, which means *anything* that is not
    ``1``/``true``/``yes``/``on`` (including ``0``, ``off``, an empty value and a
    misspelled one) selects the cached apt index rather than failing.  A ``bool``
    field would answer ``?fresh=abc`` with the binding envelope's ``400`` — a
    request this route has always served.
    """

    suites: str | None = Field(
        default=None, description="Space/comma separated suites to walk, e.g. `bookworm`"
    )
    components: str | None = Field(
        default=None, description="Space/comma separated components to walk, e.g. `main`"
    )
    arches: str | None = Field(
        default=None, description="Space/comma separated architectures to walk, e.g. `amd64`"
    )
    fresh: str | None = Field(
        default=None,
        description="`1`/`true`/`yes`/`on` bypasses the apt metadata TTL; anything else keeps it",
    )


class DebianBundleUploadForm(BaseModel):
    """``multipart/form-data`` body of ``POST /debian/offline/import``.

    One ``bundle`` part — the ``.tar.gz`` the internet-side deployment built with
    ``POST /debian/offline/bundle``.  It is a ``flask_openapi3.FileStorage`` and
    is not optional, for the reason spelled out in ``PyPIUploadForm``: only that
    schema sends the binder to ``request.files``, and ``FileStorage | None``
    sends it to the form's text fields and rejects every upload.

    Only the *import* step can bind its artifact this way.  ``plan`` and
    ``bundle`` accept the same value as a file part, a text form field **or** the
    raw request body (see ``routes/debian.py::_artifact_text``), which no single
    model describes, so those two keep reading the request by hand.
    """

    bundle: FileStorage = Field(description="The `.tar.gz` bundle to verify and unpack")


class DocsContentRequest(BaseModel):
    """JSON body of the documentation editor's write and preview routes.

    ``PUT /api/v1/docs/<ecosystem>/<doc_id>`` and its ``/preview`` sibling both
    replace one document's Markdown with ``content``; they used to share a
    ``_json_content()`` helper that read ``request.get_json(silent=True)`` and
    answered anything that was not ``{"content": "<str>"}`` with a prose ``400``.
    The binding owns that shape now — the endpoint's own ``request_body`` has
    always advertised ``required: ["content"]`` — so the helper is gone and the
    refusal arrives in the usual ``validation_error`` envelope.

    Only ``content`` is declared: the body has always been just
    ``{"content": "…"}``, and neither route reads a second key.  (The
    ``?format=`` content negotiation belongs to the server-rendered index at
    ``GET /docs/<ecosystem>/``, and ``POST /api/v1/docs/<ecosystem>`` — the
    create route — takes ``title``/``file`` parts instead of this body.)
    """

    content: str = Field(
        description="The document's full Markdown source, replacing what is stored"
    )


class DocsAssetUploadForm(BaseModel):
    """``multipart/form-data`` body of ``POST /api/v1/docs/<ecosystem>/<doc_id>/assets``.

    One ``file`` part — the image or attachment stored in the document's own
    ``assets/`` directory.  It is a ``flask_openapi3.FileStorage`` and is **not**
    optional, for the reason spelled out in ``PyPIUploadForm``: only that schema
    sends the binder to ``request.files``.

    Whether the part has a usable *filename* is checked in the view, not here:
    a same-named text field still reaches the model as a plain ``str``, and a
    part with an empty filename still parses as a file, so the binding alone
    cannot tell either from a real upload.
    """

    file: FileStorage = Field(
        description=(
            "The asset to store, e.g. a PNG the document references as "
            "`assets/<name>`"
        )
    )


class DocsDownloadQuery(BaseModel):
    """``?download=`` on ``GET /docs/<ecosystem>/<doc_id>``.

    A plain string, because the route reads it exactly as it always has
    (``bool(request.args.get("download"))``): *any* non-empty value — including
    ``0`` and ``false`` — asks for the Markdown as an attachment, and an empty
    or absent value serves it inline.  A ``bool`` field would read
    ``?download=0`` as "inline" and turn an unparseable value into a ``400``,
    neither of which this route has ever done.
    """

    download: str | None = Field(
        default=None,
        description=(
            "Any non-empty value (including `0`) serves the file as an "
            "attachment; empty or absent serves it inline"
        ),
    )


class OptionalBody(BaseModel):
    """Base for a request body a route has always accepted as *absent*.

    flask-openapi3 binds a JSON body from ``request.get_json(silent=True)`` and
    validates whatever that returns — ``None`` when the request carries no body
    at all, which every pydantic model refuses.  Three Agent-Hub routes have
    always served such a call (a sync with no body means "sync on the defaults",
    a runner ``PATCH`` with no body means "change nothing", and a finding
    ``decide`` with no body reaches the §6.1 transition table as an empty action
    and is refused there with its ``409``), and the model is also what decides
    how a body that is *not* an object is reported, so the before-validator
    below maps only that ``None`` onto the empty object those calls already
    meant.  A real body is passed through untouched, and a JSON array or string
    is still refused by pydantic.
    """

    @model_validator(mode="before")
    @classmethod
    def _absent_body_is_empty(cls, value: Any) -> Any:
        return {} if value is None else value


class RepoListQuery(BaseModel):
    """``?q=&kind=&page=&per_page=`` on ``GET /api/v1/repos``.

    ``kind`` is a plain string rather than the ``upstream``/``workspace`` enum
    the endpoint publishes: an unknown value has always filtered to an empty
    page instead of answering ``400``.

    ``page``/``per_page`` are strings for the reason :class:`DockerTagsQuery`
    documents — the view *clamps* them (``max(1, min(per_page, MAX_PER_PAGE))``)
    and reads an empty value as "use the default".  A typed ``int`` would turn
    ``?per_page=99999``, ``?per_page=`` and ``?per_page=-3`` into the binder's
    ``400``; only ``?per_page=2.5`` has ever been refused, and the route refuses
    it in its own prose.
    """

    q: str | None = Field(
        default=None,
        description="Substring match on the slug, the source URL and the Forgejo name",
    )
    kind: str | None = Field(
        default=None,
        description="`upstream` (read-only mirror) or `workspace`; an unknown value matches nothing",
    )
    page: str | None = Field(
        default=None, description="Page number; default 1, values below 1 are clamped"
    )
    per_page: str | None = Field(
        default=None,
        description="Page size; the view clamps it into `[1, MAX_PER_PAGE]` and defaults it to `DEFAULT_PER_PAGE`",
    )


class RepoIssueListQuery(BaseModel):
    """``?state=&label=&q=&is_pull_request=&page=&per_page=`` on the per-repo issue list.

    Every field is a plain string, because every filter is *parsed by the view*
    rather than by the binder:

    * ``state`` is compared literally (``all`` means "no filter") instead of
      being the enum the endpoint publishes;
    * ``is_pull_request`` goes through ``_bool_arg`` — ``true``/``yes``/``on``/``1``
      and their negatives select a value, an empty value means "unset", and
      anything else is the route's own prose ``400``;
    * ``page``/``per_page`` are clamped by ``_paging`` exactly as in
      :class:`RepoListQuery`.

    Typing any of them strictly would move a decision into the binder that this
    route has always made itself, and turn requests it tolerates into ``400``s.
    """

    state: str | None = Field(
        default=None, description="`open`, `closed` or `all`; anything else filters literally"
    )
    label: str | None = Field(
        default=None, description="One label to match exactly inside the issue's label list"
    )
    q: str | None = Field(
        default=None, description="Case-insensitive substring match on the issue title"
    )
    is_pull_request: str | None = Field(
        default=None,
        description=(
            "Boolean flag (`1`/`true`/`yes`/`on` and their negatives); an empty "
            "value keeps both issues and pull requests"
        ),
    )
    page: str | None = Field(
        default=None, description="Page number; default 1, values below 1 are clamped"
    )
    per_page: str | None = Field(
        default=None,
        description="Page size; the view clamps it into `[1, MAX_PER_PAGE]` and defaults it to `DEFAULT_PER_PAGE`",
    )


class RepoCreateRequest(BaseModel):
    """JSON body of ``POST /api/v1/repos`` — create a local workspace repository.

    Nothing is ``required``: the route's own checks, and ``services.repo_import``
    behind them, own every rule and answer with the prose ``400``/``409`` a
    caller has always seen (a slug without a ``/``, an unknown ``kind``, a
    duplicate row).  The endpoint's ``request_body`` still advertises
    ``required: ["slug"]``.

    ``description`` is deliberately absent even though the endpoint documents
    it: this route has never read it — ``services.repo_import.assign_fields``
    is handed a fixed field map — so binding it would invent behaviour rather
    than carry it over.
    """

    slug: str | None = Field(
        default=None, description='`"<owner>/<name>"`; the view refuses anything without a `/`'
    )
    default_branch: str | None = Field(
        default=None, description="Branch the workspace starts on; `main` when omitted"
    )
    kind: str | None = Field(
        default=None, description="`upstream` or `workspace` (default `workspace`)"
    )


class RepoImportRequest(BaseModel):
    """JSON body of ``POST /api/v1/repos/import``.

    ``source_url`` and ``mode`` stay optional strings so the route keeps
    answering with its own prose — ``source_url 不能为空`` and the
    ``IMPORT_MODES`` list — and so ``services.repo_import.parse_source`` stays
    the authority on what a URL may be (it refuses a non-git scheme with a
    ``400`` that names the scheme).

    ``include_prs`` is the documented boolean; ``None`` means "the default",
    which is what an omitted field has always meant.  ``repo_id`` is the
    documented integer, so an unparseable value is now the binder's ``400``
    rather than the route's prose one — the routing decision itself is unchanged.
    """

    source_url: str | None = Field(
        default=None, description="GitHub / Gitee / GitLab / any https git URL"
    )
    mode: str | None = Field(
        default=None,
        description="`code` = mirror code only; `code+issues` (default) = code, issues, labels, milestones and PRs; `issues` = issues only",
    )
    include_prs: bool | None = Field(
        default=None, description="Mirror pull requests alongside issues (default true)"
    )
    repo_id: int | None = Field(
        default=None, description="Re-import into this existing repository instead of creating one"
    )


class RepoSyncRequest(OptionalBody):
    """JSON body of ``POST /api/v1/repos/<slug>/sync`` — absent by default.

    The endpoint publishes ``required: False`` and that is the behaviour: a sync
    with no body at all means "sync on the defaults", and the route has always
    answered it.  See :class:`OptionalBody` for why that needs the inherited
    before-validator.

    ``include_prs`` and ``full`` are booleans whose *absence* keeps the route's
    old defaults: pull requests are mirrored, and the job resumes on the existing
    Forgejo mirror instead of re-running the migration.
    """

    mode: str | None = Field(
        default=None, description="`code`, `code+issues` (default) or `issues`"
    )
    include_prs: bool | None = Field(
        default=None, description="Mirror pull requests alongside issues (default true)"
    )
    full: bool | None = Field(
        default=None,
        description=(
            "Re-run migration as well as the issue/commit mirror. The default "
            "(false) resumes mirroring on the existing Forgejo repo."
        ),
    )


class RunnerPatchRequest(OptionalBody):
    """JSON body of ``PATCH /api/v1/repos/<slug>/runner``.

    Every field is optional and only the keys actually present are forwarded to
    ``services.repo_runner.RepoRunnerService.update``
    (``model_dump(exclude_unset=True)``): the service reads an absent keyword as
    "leave the stored value", so dumping every field would reset the settings
    the operator did not mention.  A bodyless ``PATCH`` means the same as ``{}``
    — see :class:`OptionalBody`.

    ``max_concurrency`` carries no ``ge=0`` even though the endpoint publishes
    ``minimum: 0``: the *service* refuses a negative value with its own prose
    ``400``, and it validates everything before it writes anything.  Likewise
    ``egress_policy`` is a plain string — the accepted set lives in
    ``services.repo_runner.RUNNER_EGRESS_POLICIES`` and is enforced there.
    """

    enabled: bool | None = Field(
        default=None, description="Disabled runners stay configured but are not dispatched to"
    )
    max_concurrency: int | None = Field(
        default=None, description="0 = inherit `AGENT_MAX_IN_FLIGHT_PER_REPO`"
    )
    egress_policy: str | None = Field(
        default=None, description="`inherit`, `internal` or `allowlist` (service-validated)"
    )
    egress_allowlist: str | None = Field(
        default=None, description="Comma-separated hosts; an empty string clears the allowlist"
    )
    workspace_subdir: str | None = Field(
        default=None,
        description="Relative path under `AGENT_WORK_ROOT`; an empty string restores the `runners/<id>` default",
    )


class RunnerCredentialRequest(BaseModel):
    """JSON body of ``PUT /api/v1/repos/<slug>/runner/credential``.

    ``token`` is required (the endpoint publishes ``required: ["token"]``) and
    the view keeps its emptiness check, because ``""`` and ``"   "`` satisfy
    ``str`` — the service refuses them too, but the route has always answered
    them itself, before it asks the service to seal anything.

    ``expires_at`` stays a string: ``_credential_expiry`` in ``routes/repos.py``
    owns the ISO-8601 parse and its prose ``400``, which is also what an empty
    value gets.  ``username`` is optional, and ``null`` means the same as
    omitted — the conventional git login.
    """

    token: str = Field(
        description=(
            "Forgejo access token. Sealed with the runner credential key; "
            "**never** returned, logged or stored in clear."
        )
    )
    username: str | None = Field(
        default=None, description="Optional; the conventional git username when omitted"
    )
    expires_at: str | None = Field(
        default=None, description="Optional ISO-8601 UTC expiry of the token"
    )


class RepoContextSearchQuery(BaseModel):
    """``?q=&finding_id=&kind=&…`` on ``GET /api/v1/repos/<slug>/context/search``.

    Every field is a plain string, because every one of them is *parsed by the
    view* rather than by the binder — the same discipline :class:`RepoListQuery`
    and :class:`RepoIssueListQuery` document:

    * ``finding_id``/``limit``/``offset``/``budget`` go through the route's own
      integer readers, which read an absent **or empty** value as "not given"
      (an empty ``?limit=`` is the default, an empty ``?finding_id=`` is keyword
      mode) and answer anything unparseable with the route's prose ``400``;
    * ``limit``/``offset``/``budget`` are additionally *clamped* by those
      readers, so ``?budget=999999`` keeps answering with the maximum instead of
      the binding's ``400``;
    * ``is_pull_request`` is tri-state through ``_bool_arg``: ``1``/``true``/
      ``yes`` and their negatives select a value, an empty value means "unset",
      and anything else is the route's prose ``400``;
    * ``since``/``until`` are ISO-8601 timestamps parsed in the view;
    * ``kind`` is checked against ``services.repo_context``'s own ``KINDS``, so
      an unknown value stays the route's prose ``400`` that names the accepted
      set (the endpoint publishes the enum);
    * ``state``/``label``/``author`` are passed through verbatim.

    Typing any of them strictly would move a decision into the binder that this
    route has always made itself, and turn requests it tolerates into ``400``s.
    """

    q: str | None = Field(
        default=None,
        description=(
            "Keyword query, title-weighted; whitespace-separated terms are AND-ed "
            "and a Chinese run stays one term. Mutually exclusive with `finding_id`."
        ),
    )
    finding_id: str | None = Field(
        default=None,
        description="Start from one finding's linked history instead of a keyword",
    )
    kind: str | None = Field(
        default=None,
        description="`issue` / `commit` / `finding` / `all`; an unknown value is the route's prose 400",
    )
    state: str | None = Field(
        default=None, description="Mirrored issue/PR state (`open`/`closed`)"
    )
    label: str | None = Field(
        default=None, description="Exact match inside the issue's JSON label array"
    )
    author: str | None = Field(
        default=None, description="Issue/PR/commit author, case-insensitive exact match"
    )
    is_pull_request: str | None = Field(
        default=None,
        description=(
            "Tri-state flag (`1`/`true`/`yes`/`on` and their negatives); an empty "
            "value keeps both issues and pull requests"
        ),
    )
    since: str | None = Field(
        default=None, description="Inclusive ISO-8601 lower bound on the creation time"
    )
    until: str | None = Field(
        default=None, description="Inclusive ISO-8601 upper bound on the creation time"
    )
    limit: str | None = Field(
        default=None,
        description="Top-k per kind; the view clamps it into the service's range",
    )
    offset: str | None = Field(
        default=None, description="Row offset for paging the underlying matches"
    )
    budget: str | None = Field(
        default=None,
        description="Character budget for `text`; the view clamps it into the service's range",
    )


class FindingListQuery(BaseModel):
    """``?repo=&status=&level=&rule=&owner=&limit=&offset=`` on ``GET /api/v1/findings``.

    The five filters are plain strings because the *service* resolves them:
    ``repo`` accepts a numeric id or a ``<owner>/<name>`` slug, and an unknown
    ``status``/``level`` has always filtered to an empty page rather than
    answering ``400`` — the endpoint publishes the enums, this route does not
    enforce them.

    ``limit``/``offset`` are strings for the reason :class:`DockerTagsQuery`
    documents: ``_int_arg`` in ``routes/findings.py`` reads an absent **or
    empty** value as the default (``?limit=`` means 200) and answers anything
    else that is not an integer with the route's own prose ``400``.  Neither is
    clamped here — the board asks for what it asks for.
    """

    repo: str | None = Field(
        default=None, description="Filter by repository: a numeric id or a `<owner>/<name>` slug"
    )
    status: str | None = Field(
        default=None, description="`open`/`acknowledged`/`wontfix`/`fixed`/`stale`; an unknown value matches nothing"
    )
    level: str | None = Field(
        default=None, description="`blocking` or `debt`; an unknown value matches nothing"
    )
    rule: str | None = Field(default=None, description="Exact `rule_id` match")
    owner: str | None = Field(default=None, description="Filter by the deferral's owner")
    limit: str | None = Field(
        default=None, description="Rows to return (default 200); a non-integer is the route's prose 400"
    )
    offset: str | None = Field(
        default=None, description="Rows to skip (default 0); a non-integer is the route's prose 400"
    )


class FindingDecisionRequest(OptionalBody):
    """JSON body of ``POST /api/v1/findings/<finding_id>/decide`` — one §6.1 transition.

    ``action`` is a plain string rather than the enum the endpoint publishes:
    an unknown or missing action is the **service's** ``409
    finding_decision_invalid``, the §6.1 table's own answer, and an enum here
    would replace that answer with the binder's ``400``.  For the same reason
    ``owner``/``due`` carry no rule of their own — I2 (both required for
    ``acknowledge``/``wontfix``) and I3 (a ``blocking`` finding accepts neither)
    live in ``services.findings.decide``, and ``findings.coerce_due`` keeps the
    ISO-8601 parse and its prose ``400``.

    :class:`OptionalBody` because this route has always served a bodyless call.
    The view read ``request.get_json(silent=True) or {}``, so a bodyless
    ``POST`` reached the service as an empty action and came back as that
    ``409``; without the inherited before-validator the binder would answer the
    same call with its own ``400`` — a different status for a request this route
    has always answered.
    """

    action: str | None = Field(
        default=None,
        description=(
            "`fix`/`fixed` = it is (or will be) repaired; `acknowledge`/`wontfix` "
            "= deferred to `owner`+`due` (debt only — I2/I3); `false_positive` = "
            "a confirmed `fixed` that also feeds the §6.3 rule review"
        ),
    )
    owner: str | None = Field(default=None, description="Required for acknowledge/wontfix (I2)")
    due: str | None = Field(
        default=None,
        description="ISO date, required for acknowledge/wontfix, must be in the future",
    )
    reason: str | None = Field(
        default=None, description="`false_positive` marks a confirmed rule defect (§6.3)"
    )
    confirmed_by: str | None = Field(
        default=None, description="Second person confirming a false positive"
    )


class ReviewPolicyRequest(BaseModel):
    """JSON body of ``PUT /api/v1/policies/<slug>`` — the §7.2 document.

    Permissive on purpose: ``services.review_policy.PolicyDocument`` (which
    forbids unknown keys) is the only validator this document has ever had, and
    its prose ``400`` — ``policy schema violation: …`` for a wrong type, and the
    §7.3 rules for a missing or expired ``exceptions[*].due`` — is the answer a
    caller sees.  Declaring the sections' shapes here would answer some of those
    documents with the binder's envelope instead, and would move §7.3 out of the
    one place that implements it.

    ``extra="allow"`` plus ``model_dump(by_alias=True, exclude_unset=True)`` in
    the view is load-bearing: that mapping goes straight to ``parse_document``,
    so an **absent** section must stay absent (it takes the built-in default)
    while an explicit ``null`` must stay ``null`` — that one *is* a validation
    failure.  Dumping every field would turn each omitted section into ``None``
    and refuse a document this route has always accepted.

    ``checks`` is declared although the endpoint's own ``request_body`` lists
    only the five documented sections, because ``.agent/checks/`` accepts an
    inline command suite: a document carrying one must keep reaching the service
    (``extra="allow"`` would carry it either way; the field documents it).
    """

    model_config = ConfigDict(extra="allow")

    version: Any = Field(default=None, description="Document revision; 1 when omitted")
    defaults: Any = Field(
        default=None, description="Run-level switches (`auto_review`, `max_findings_per_run`, …)"
    )
    rules: Any = Field(default=None, description="Per-rule policy entries, each with an `id`")
    exceptions: Any = Field(
        default=None, description="Time-boxed reprieves; each needs a future `due` (§7.3)"
    )
    escalation: Any = Field(
        default=None, description="`rule_noise_threshold` / `rule_count_threshold`"
    )
    checks: Any = Field(
        default=None, description="Inline verification commands (the `.agent/checks/` escape hatch)"
    )


class AgentTaskListQuery(BaseModel):
    """``?repo=&status=&page=&page_size=`` on ``GET /api/v1/agent/tasks``.

    ``repo``/``status`` are handed to the view as written: the repository slug is
    matched literally by the join, and ``status`` is checked against
    ``models.agent_hub.TASK_STATUS`` by the route itself, whose prose ``400``
    names the accepted set (the endpoint publishes the enum).

    ``page``/``page_size`` are strings because the view *clamps* them
    (``max(1, …)`` and ``min(…, _MAX_PAGE_SIZE)``): a ``ge=1``/``le=100`` here
    would turn ``?page_size=5000``, which this route has always served as 100,
    into the binder's ``400``.  This route is stricter than the hub catalogs
    about a *malformed* value — its ``_int_arg`` reads an absent value as the
    default and answers ``?page=``/``?page=abc`` with its own prose ``400`` — so
    the raw string is what the model carries and the decision stays in the view.
    """

    repo: str | None = Field(default=None, description="Filter by repository slug")
    status: str | None = Field(
        default=None, description="`queued`/`leased`/`running`/`done`/`failed`/`dead`; an unknown value is the route's prose 400"
    )
    page: str | None = Field(
        default=None, description="Page number; default 1, values below 1 are clamped"
    )
    page_size: str | None = Field(
        default=None, description="Page size; default 20, clamped into `[1, _MAX_PAGE_SIZE]`"
    )


class AgentTaskCreateRequest(BaseModel):
    """JSON body of ``POST /api/v1/agent/tasks`` — ``{repo, kind, payload, …}``.

    Nothing is ``required`` and the three value fields stay untyped, because
    every rule this route applies is the view's (and, behind it,
    ``services.agent_queue.AgentQueue.enqueue``'s):

    * ``repo`` is resolved to a row by **slug**; a missing one is the route's
      prose ``400`` (``缺少 repo``) and an unknown one its ``404``;
    * ``kind`` is checked against ``models.agent_hub.TASK_KIND`` in the view, so
      an unknown kind keeps the prose ``400`` that names the accepted values;
    * ``payload or {}`` means **every** empty value — ``null``, ``""`` and ``[]``
      alike — is the empty payload, and a non-object is the route's prose ``400``
      (``payload 必须是 JSON 对象``).  A ``dict`` field would answer
      ``{"payload": []}``, which this route has always served as an empty
      payload, with the binder's ``400``;
    * ``priority``/``max_attempts`` are read through ``_coerce_int``, which
      applies the queue's own defaults (``0``/``3``) and accepts anything
      ``int()`` accepts (``3.0``, ``"3"``, ``true``).  ``max_attempts``' real
      floor is the view's prose ``400`` (``max_attempts 至少为 1``) and the
      queue's ``ValueError`` behind it.

    Typing those fields would move a decision into the binder that this route has
    always made itself — and two of the answers (an unknown repository, a
    non-object payload) are not ``400``s at all.
    """

    repo: str | None = Field(
        default=None, description="Repository slug (`<owner>/<name>`); required in practice"
    )
    kind: str | None = Field(
        default=None, description="`review`/`fix`/`import`/`backfill`; required in practice"
    )
    payload: Any = Field(
        default=None, description="Target sha / issue number / finding ids; the empty object when omitted"
    )
    priority: Any = Field(default=None, description="Higher is claimed first; default 0")
    max_attempts: Any = Field(default=None, description="Default 3, never below 1")


# ── Package file ─────────────────────────────────────────────────────

@dataclass
class PackageFile:
    filename: str
    path: str
    url: str
    package_name: str
    version: str
    packagetype: str = "sdist"
    python_version: str = "source"
    requires_python: str | None = None
    size: int = 0
    upload_time: str | None = None
    md5_digest: str | None = None
    sha256_digest: str | None = None


# ── Python-build file ────────────────────────────────────────────────

@dataclass
class PythonBuildFile:
    filename: str
    path: str
    release_tag: str       # e.g. "20260602"
    flavor: str            # "cpython"
    version: str           # e.g. "3.12.13"
    full_version: str      # e.g. "cpython-3.12.13+20260602"
    target_triple: str     # e.g. "aarch64-unknown-linux-gnu"
    variant: str           # e.g. "install_only_stripped"
    extension: str         # e.g. "tar.gz"
    size: int = 0
    sha256_digest: str | None = None


# ── Node-build file ──────────────────────────────────────────────────

@dataclass
class NodeBuildFile:
    filename: str
    path: str
    release_tag: str       # e.g. "v20.11.0"
    version: str           # e.g. "20.11.0"
    platform: str          # "linux" | "darwin" | "win" | "headers" | ...
    arch: str              # "x64" | "arm64" | ... — empty for `headers`
    extension: str         # e.g. "tar.xz"
    size: int = 0
    sha256_digest: str | None = None


# ── Sort helpers ─────────────────────────────────────────────────────

OS_ORDER = {
    "apple-darwin": "macOS",
    "unknown-linux-gnu": "Linux (glibc)",
    "unknown-linux-musl": "Linux (musl)",
    "pc-windows-msvc": "Windows",
}

ARCH_ORDER = {
    "x86_64": 0, "aarch64": 1, "armv7": 2,
    "i686": 3, "ppc64le": 4, "riscv64": 5, "s390x": 6,
}

VARIANT_ORDER = {
    "install_only_stripped": 0, "install_only": 1,
    "pgo+lto-full": 2, "lto-full": 3,
    "pgo-full": 4, "debug-full": 5, "noopt-full": 6,
}


# ── Response models ──────────────────────────────────────────────────
# Every shape the JSON API can return.  Kept next to the request models so the
# whole wire contract lives in one file.

class ErrorResponse(BaseModel):
    """Body of every JSON error response."""

    error: str = Field(description="Human readable failure reason")


class SessionInfo(BaseModel):
    """Result of `GET /api/v1/session`.

    Returned with HTTP 200 even for an anonymous caller, so a client can render
    a signed-out state without treating it as an error.
    """

    authenticated: bool = Field(description="Whether a usable credential was presented")
    auth_enabled: bool = Field(description="Whether this deployment enforces authentication")
    user: str | None = Field(
        description="Stable account id of the credential (never a display name), or null"
    )
    display_name: str | None = Field(
        default=None, description="Human-facing name for `user`, when known"
    )
    role: str = Field(description="Primary role code — admin | authenticated | anonymous | custom")
    roles: list[str] = Field(
        default_factory=list, description="Every role code the account holds"
    )
    permissions: list[str] = Field(
        description="Effective permission strings, resolved from the role tables"
    )
    server_name: str = Field(description="Configured branding name")
    is_admin: bool = Field(description="Shorthand for the admin:view permission")
    is_superuser: bool = Field(
        default=False,
        description="True when the account bypasses permission checks entirely",
    )
    auth_method: str | None = Field(description="Which credential was accepted")


class KeyStatRow(BaseModel):
    """One (package, event) bucket of API key usage."""

    package_name: str
    event_type: str = Field(description="download | upload")
    count: int


class KeyStats(BaseModel):
    """Per-key usage breakdown."""

    key_id: str
    total_downloads: int
    total_uploads: int
    per_package: list[KeyStatRow]


class ApiKey(BaseModel):
    """An issued API key.  The secret itself is never stored or returned here."""

    id: str = Field(description="Stable identifier, e.g. k_9f2c1a4b7d3e")
    name: str = Field(description="Human readable label")
    prefix: str = Field(description="First characters of the raw key, for identification")
    created_by: str
    created_at: str = Field(description="ISO 8601 UTC")
    expires_at: str | None = Field(description="ISO 8601 UTC, or null when permanent")
    is_permanent: bool
    is_expired: bool
    last_used: str | None = Field(description="ISO 8601 UTC, or null if never used")
    download_count: int
    upload_count: int
    stats_detail: KeyStats | None = None


class CreatedApiKey(ApiKey):
    """Response of `POST /api/v1/keys` — the only time the secret is shown."""

    key: str = Field(
        description="Raw API key. Returned exactly once; only its SHA-256 is stored.",
    )


class CreateKeyRequest(BaseModel):
    """Body of `POST /api/v1/keys`."""

    name: str = Field(
        min_length=1,
        max_length=128,
        description="Human readable label, e.g. 'ci-pipeline'",
    )
    expires_in_days: int | None = Field(
        default=None,
        description=(
            "Lifetime in days. Omit, or send null / a non-positive value, for a "
            "key that never expires."
        ),
    )


class DeleteResult(BaseModel):
    """Body of a successful delete."""

    deleted: str = Field(description="Identifier of the removed resource")


# ── Device authorization (the DSH plugin's key hand-off) ─────────────

class DeviceCodeResponse(BaseModel):
    """Result of `POST /api/v1/device/code` — one pending request."""

    device_code: str = Field(
        description=(
            "Secret bearer of the exchange. Returned exactly once and stored "
            "only as a SHA-256 hash; present it to `POST /api/v1/device/token`."
        ),
    )
    user_code: str = Field(
        description="Short human code, shown in DSH and confirmed on the approval page",
    )
    verification_uri: str = Field(description="Absolute approval page URL")
    verification_uri_complete: str = Field(
        description="The same URL with `?user_code=` pre-filled",
    )
    expires_in: int = Field(description="Seconds until the request is abandoned")
    interval: int = Field(description="Minimum seconds between token polls")


class DeviceTokenRequest(BaseModel):
    """Body of `POST /api/v1/device/token`."""

    device_code: str = Field(min_length=1, description="From the code response")


class DeviceTokenResponse(BaseModel):
    """Result of an approved `POST /api/v1/device/token`.

    The request is consumed by this response: the same `device_code` can never
    yield the key twice.
    """

    api_key: str = Field(description="Raw API key — capture it now, it is shown once")
    key_id: str = Field(description="Stable identifier of the minted key")
    key_name: str | None = Field(default=None, description="Label recorded on the key")
    key_prefix: str | None = Field(default=None, description="Non-secret prefix of the key")
    key_expires_at: str | None = Field(
        default=None, description="ISO 8601 UTC, or null when permanent"
    )
    user: str | None = Field(default=None, description="Account that approved the request")
    display_name: str | None = Field(default=None, description="Human name of that account")
    platform_url: str = Field(description="Base URL of this server")
    token_type: str = Field(default="Bearer", description="How to present `api_key`")


class DeviceTokenError(BaseModel):
    """Body of a 400 from `POST /api/v1/device/token`."""

    error: str = Field(
        description="authorization_pending | expired_token | invalid_request",
    )
    error_description: str = Field(description="Human readable reason")
    interval: int | None = Field(
        default=None, description="Present on `authorization_pending`"
    )


class PackageSummary(BaseModel):
    """One package in `GET /api/v1/packages`."""

    name: str = Field(description="Normalised (PEP 503) package name")
    file_count: int
    total_size: int = Field(description="Bytes")
    total_size_human: str = Field(description="Pre-formatted, e.g. '12.4 MB'")
    download_count: int
    upload_count: int


class AdminOverview(BaseModel):
    """Aggregate counters across the whole server."""

    package_count: int
    file_count: int
    total_storage: int
    total_storage_human: str
    total_keys: int
    active_keys: int
    total_downloads: int
    total_uploads: int


class AdminKeyEntry(BaseModel):
    """One API key as summarised in the admin statistics."""

    id: str
    name: str
    prefix: str
    created_by: str
    download_count: int
    upload_count: int
    last_used: str = ""
    created_at: str = ""
    expires_at: str = ""
    is_expired: bool
    is_permanent: bool


class AdminStats(BaseModel):
    """Result of `GET /api/v1/admin/stats`."""

    overview: AdminOverview
    packages: list[PackageSummary]
    keys: list[AdminKeyEntry]


class DatabaseInfo(BaseModel):
    """Which backend the API keys / RBAC / statistics layer is bound to."""

    dialect: str = Field(description="sqlite | postgresql")
    driver: str = Field(description="SQLAlchemy driver, e.g. pysqlite / psycopg")


class HealthInfo(BaseModel):
    """Result of `GET /health`."""

    status: str
    server: str
    package_count: int
    packages_dir: str
    database: DatabaseInfo


class BuildMirrorHealth(BaseModel):
    """Result of `GET /python-builds/health`."""

    status: str = Field(description="ok | disabled")
    reason: str | None = Field(default=None, description="Present when disabled")
    builds_dir: str | None = None
    releases: int | None = None
    files: int | None = None
    total_size: int | None = None
    total_size_human: str | None = None
    flavors: list[str] | None = None
    versions: list[str] | None = None


class BuildChecksum(BaseModel):
    """Result of `GET /python-builds/<tag>/<filename>/sha256`."""

    filename: str
    release_tag: str
    version: str
    sha256: str
    size: int


class SimpleProject(BaseModel):
    """One project in the PEP 691 flat project list."""

    name: str


class SimpleIndexJson(BaseModel):
    """PEP 691 `application/vnd.pypi.simple.v1+json` project list."""

    meta: dict
    projects: list[SimpleProject]


class SimpleFile(BaseModel):
    """One downloadable file in the PEP 691 per-project document."""

    filename: str
    url: str
    hashes: dict[str, str] = Field(description="At least sha256")
    requires_python: str = Field(alias="requires-python")
    size: int
    upload_time: str | None = Field(default=None, alias="upload-time")


class SimpleProjectJson(BaseModel):
    """PEP 691 `application/vnd.pypi.simple.v1+json` per-project document."""

    meta: dict
    name: str
    files: list[SimpleFile]


# ── Access control (roles, permission points, accounts) ───────────────
# Backed by the five RBAC tables: users / roles / permissions /
# user_roles / role_permissions.  See services/authz.py.

class RoleInfo(BaseModel):
    """One role and the permission points it currently holds."""

    id: int
    code: str = Field(description="Stable identifier, e.g. `publisher`")
    name: str = Field(description="Display name, editable by an administrator")
    description: str | None = None
    is_builtin: bool = Field(description="Built-in roles cannot be deleted")
    is_anonymous_default: bool = Field(
        description="Granted to requests that never authenticated"
    )
    auto_grant: bool = Field(
        description="Granted automatically to every account on first login"
    )
    permissions: list[str] = Field(
        default_factory=list, description="Permission codes this role holds"
    )
    user_count: int = Field(default=0, description="Accounts holding this role")


class PermissionInfo(BaseModel):
    """One permission point — a code some route guard checks."""

    id: int
    code: str = Field(description="`module:action`, e.g. `package:write`")
    name: str
    module: str | None = Field(default=None, description="UI grouping key")
    description: str | None = None
    role_count: int = Field(
        default=0,
        description="How many roles hold this point. 0 means no code path can reach "
                    "it except through a superuser — usually a typo.",
    )
    stale: bool = Field(
        default=False,
        description="The row exists but no guard declares the code any more, so "
                    "granting it changes nothing. Usually a renamed or removed point.",
    )
    expected_for_authenticated: bool = Field(
        default=False,
        description="The seed data hands this point to the auto-granted "
                    "`authenticated` role.",
    )
    held_by_authenticated: bool = Field(
        default=False,
        description="The `authenticated` role holds it right now.",
    )
    authenticated_pending: bool = Field(
        default=False,
        description="Expected for ordinary users but not granted to them yet — the "
                    "`authenticated` role needs the migration that ships with a new "
                    "point. Invisible in `role_count`, because `admin` is topped up "
                    "automatically and hides the gap.",
    )


class UserInfo(BaseModel):
    """One local account (a shadow of an externally authenticated identity)."""

    id: int
    provider: str = Field(description="How the account was first seen")
    external_id: str = Field(description="Stable identity — what roles attach to")
    display_name: str | None = None
    email: str | None = None
    is_active: bool
    is_superuser: bool = Field(description="Bypasses every permission check")
    roles: list[str] = Field(default_factory=list)
    last_login_at: str | None = None
    created_at: str | None = None


class CreateRoleRequest(BaseModel):
    """Body of `POST /api/v1/admin/roles`."""

    code: str = Field(description="Letters, digits, '-' and '_' only")
    name: str = Field(default="", description="Defaults to `code` when empty")
    description: str | None = None


class SetRolePermissionsRequest(BaseModel):
    """Body of `PUT /api/v1/admin/roles/{role_id}/permissions`.

    Replaces the role's grants wholesale.  Codes that do not exist yet are
    created as new permission points, so a permission can be defined before the
    code that checks it ships.
    """

    permissions: list[str]


class GrantRoleRequest(BaseModel):
    """Body of `POST /api/v1/admin/users/{user_id}/roles`."""

    role: str = Field(description="Role code to grant")


class SetSuperuserRequest(BaseModel):
    """Body of `PUT /api/v1/admin/users/{user_id}/superuser`."""

    superuser: bool


# ── Agent runtime ────────────────────────────────────────────────────

class AgentTask(BaseModel):
    """One agent task as `/api/v1/agent/tasks` returns it.

    ``runner_id`` names the logical per-repository runner
    (``repo_runners.id``) that owns the task.  It is ``null`` for a legacy row
    that was queued before runners existed (``agent_tasks.runner_id IS NULL``),
    which is why the console must treat it as optional.
    """

    id: int
    repo: str | None = Field(default=None, description="仓库 slug")
    repo_id: int
    runner_id: int | None = Field(
        default=None,
        description=(
            "repo_runners.id — the logical runner that owns this task; null for "
            "a task queued before runners existed"
        ),
    )
    kind: str
    status: str
    attempts: int
    max_attempts: int
    priority: int
    result_ref: str | None = None
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
