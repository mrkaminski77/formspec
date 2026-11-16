# FormSpec Language Specification (v0.1 Draft)

**FormSpec** (`.fspec`) is a declarative, Markdown-inspired markup language for building form-based applications. It unifies form structure, field types, validation, defaults, role-based permissions, layout grouping, file uploads, and reactive entity-backed queries in a single human-readable source format.

---

## 1. Design Goals

1. **Concise, Markdown-like syntax** for common controls (`[Label](id:type)`, `[x] Label (id)`, `[[Submit]](id)`).
2. **First-class RBAC permissions** (`@(role:access, ...)`) at form, block/group, and field levels with deterministic inheritance and overrides.
3. **Composable grouping and layout** via `:::` multi-line grouping blocks with tab-based nesting and `|` inline layout hints with optional `[ratio]` specs.
4. **Domain-entity integration** via `->` bindings, scoped queries, and reactive cross-field dependencies (`$field_id`).
5. **Runtime-agnostic semantics** suitable for compilation to web UIs (React, Vue, HTML), mobile forms, and server-side validation/authorization handlers.

---

## 2. Lexical Structure

- **Encoding**: UTF-8.
- **Line Endings**: LF (`\n`) or CRLF (`\r\n`).
- **Whitespace & Tab-Based Nesting**:
  - Nesting depth is determined strictly by leading **tab characters (`\t`, U+0009)** at the start of each physical line (`depth = number of leading '\t'`).
  - Leading spaces (`' '`) or mixed tab/space indentation prefixes are a compile-time **indentation error**.
  - Spaces and tabs inside a line separate tokens normally.
  - Multi-line expressions inside balanced delimiters (`(...)`, `{...}`) do not trigger dedent/block termination until the matching closing delimiter.
  - Blank lines (or whitespace-only lines) do not terminate an open block.
- **Comments**:
  - Line comment: `// ...` to end of line.
  - Block comment: `/* ... */` (may span multiple lines; non-nesting).
- **Identifiers**:
  ```ebnf
  ident = ( ALPHA | "_" ) , { ALPHA | DIGIT | "_" } ;
  ```
- **Literals**:
  - **String**: `"..."` or `'...'` with standard backslash escapes (`\"`, `\'`, `\\`, `\n`, `\t`).
  - **Number**: integer (`42`, `-10`) or decimal (`3.14`), optionally with unit suffix for sizes/durations (`10MB`, `500KB`, `30d`).
  - **Boolean**: `true` | `false`.
  - **Null**: `null`.
  - **Date / Time**: ISO-8601 literals (`2026-11-16`, `2026-11-16T09:00:00Z`) or built-in runtime tokens (`$today`, `$now`).
  - **Array**: `[ expr , ... ]`.
- **Reserved Sigils & Operators**:
  - `*` — Required field marker (before label), or wildcard role inside `@(...)`, or multiplication in expressions.
  - `=` — Default value assignment.
  - `==`, `!=`, `<`, `<=`, `>`, `>=` — Comparison operators in queries and conditions.
  - `->` — Entity link / query binding inside `{...}`.
  - `$` — Context or form-field variable reference (e.g., `$dept_id`, `$user.org_id`).
  - `@(...)` — Permission sigil.
  - `:::` — Multi-line grouping block header prefix (body scoped by `\t` nesting).
  - `|` — Statement-level horizontal layout separator (with optional trailing `| [ratios]`), or presentation-hint delimiter inside `{...}`.

---

## 3. Document Structure

A `.fspec` file defines a form (or multiple named forms). If no explicit `::: form` block is present, the entire file forms an implicit root form.

Forms have two primary types via `(type: detail | list)` (defaulting to `detail`):
- **Detail Form** (`type: detail`, default): Creates, views, or updates a single entity record.
- **List Form** (`type: list`): Displays an optionally filtered table of entity records via `::: list <entity_query>` with row links to open the corresponding detail form (`id = $row.id`).

```formspec
// Form header with tab-nested children
::: form leave_request "Leave Request" @(employee:write, manager:read, hr:write, *:none)
	## Employee Information
	[*Employee Name](employee_name:text = $user.name) | [*Department]{dept_id -> Department(org_id == $user.org_id).order(name asc)} | [1:1]
	[*Start Date](start_date:date = $today) | [*End Date](end_date:date)

	[[Submit]](submit) @(employee:read, hr:write, *:none)
```

A document may contain:
- **Markdown-style headings & prose**: `#`, `##`, `###`, plain text paragraphs, and `---` horizontal rules.
- **Form elements**: inputs, choices, checkboxes, file uploads, and buttons.
- **Container blocks**: `::: section`, `::: group`, `::: row`, `::: grid`, `::: col`, `::: tabs`, `::: tab`, `::: repeat`, `::: list`.

---

## 4. Form Elements

### 4.1 Typed Input Fields

**Syntax**:
```
[*]?[Label](field_id[:type[(constraints)]] [= default_expr]) [modifiers...]
```

- `*` immediately before `Label` marks the field as **required** (`\*` escapes a literal leading asterisk).
- `:type` defaults to `text` when omitted.
- `=` sets a default value expression evaluated on form initialization.

**Built-in Types**:

| Type | Description | Example Constraints |
| :--- | :--- | :--- |
| `text` | Single-line string (default) | `min`, `max`, `pattern` |
| `textarea` | Multi-line string | `min`, `max`, `rows` |
| `password` | Masked secret input | `min`, `max` |
| `email` | Email address | `domain` |
| `url` | HTTP/HTTPS URL | `schemes` |
| `tel` | Telephone number | `pattern` |
| `int` | Integer | `min`, `max`, `step`, range `0..100` |
| `number` / `decimal` | Floating-point / decimal number | `min`, `max`, `precision`, range `0..1` |
| `currency` | Monetary amount | `min`, `max`, `currency="USD"` |
| `date` | Calendar date | `min`, `max` |
| `time` | Time of day | `min`, `max` |
| `datetime` | Timestamp with timezone | `min`, `max` |
| `color` | Hex color | — |
| `hidden` | Non-rendered value carrier | — |
| `file` | File upload (see Section 4.5) | `accept`, `max_size`, `multiple`, `max_files` |

**Examples**:
```formspec
[*Full Name](full_name:text(min=2, max=120))
[Nickname](nickname)
[*Start Date](start_date:date = $today)
[Work Email](work_email:email)
[Hours Requested](hours:int(1..80) = 8)
[Notes](notes:textarea(rows=4, max=1000))
```

---

### 4.2 Selection / Choice Fields

**Syntax**:
```
[*]?[Label]{field_id[[]][= default_expr] : option_list [| presentation]} [modifiers...]
```

- Single-select by default; append `[]` to `field_id` for **multi-select** (whose value is an array).
- Options can be bare tokens (`opt1`) or value-with-label (`val("Display Label")`).
- Optional `| presentation` hints: `select` (default for >4 options), `radio` (single-select), `checkboxes` (multi-select), `chips`.

**Examples**:
```formspec
// Single select with default
[*Priority]{priority = medium : low, medium, high}

// Value and display label pairs
[Region]{region = apac : na("North America"), emea("Europe & Middle East"), apac("Asia-Pacific")}

// Multi-select with presentation hint
[Notification Channels]{channels[] = ["email"] : email("Email"), slack("Slack"), sms("SMS") | checkboxes}
```

---

### 4.3 Checkboxes (Boolean Controls)

**Syntax**:
```
[ ] [*]?Label (field_id [= default_bool]) [modifiers...]
[x] [*]?Label (field_id [= default_bool]) [modifiers...]
```

- `[ ]` initializes the boolean field to `false`; `[x]` or `[X]` initializes it to `true`.
- If `= default_expr` is also supplied inside `(...)`, `= default_expr` takes precedence (or `[ ]` / `[x]` is omitted in favor of `=`).
- `*` immediately before `Label` marks the checkbox as required (must evaluate to `true` on submission, e.g., legal consent).

**Examples**:
```formspec
[ ] Subscribe to release notes (subscribe)
[x] Enable email alerts (email_alerts)
[ ] *I agree to the Terms of Service (accept_tos)
```

---

### 4.4 Buttons & Actions

**Syntax**:
```
[[Label]](action_id[:kind] [-> target_form[(link_args)]]) [modifiers...]
```

- `kind` is one of:
  - `submit` — Validates visible/writable fields and submits form payload to the target handler. (Default when `kind` is omitted and no `->` link is present, unless `:action`, `:reset`, or `:link` is specified.)
  - `reset` — Restores form fields to their initial/default values.
  - `action` — Triggers a custom client/server action (e.g., draft save, lookup, export) without full form submission.
  - `link` — Navigates to or opens another linked form without submitting the current form (default when `->` is present and `:kind` is omitted; see Section 9).

**Examples**:
```formspec
[[Submit]](submit)
[[Save Draft]](save_draft:action)
[[Reset]](reset:reset)
[[Add Department]](add_dept -> department(mode: "modal", returns: $department_id))
```

---

### 4.5 File Uploads

File uploads use the typed input syntax with `:file` (or `:files` as shorthand for `multiple=true`).

**Syntax**:
```
[*]?[Label](field_id:file[(upload_constraints)]) [modifiers...]
```

**Upload Constraints**:
- `accept`: string or list of MIME types / extensions (e.g., `accept=[".pdf", ".docx", "image/*"]`).
- `max_size`: maximum size per file (e.g., `max_size=10MB`).
- `multiple`: boolean (`true` | `false`, default `false`).
- `min_files` / `max_files`: integer bounds when `multiple=true`.

**Value Representation**:
A single `file` field binds to a file metadata/handle object (or `null`); a `multiple=true` field binds to an array of file objects.

**Examples**:
```formspec
[*Resume](resume:file(accept=[".pdf", ".docx"], max_size=5MB))
[Supporting Documents](docs:file(accept=["application/pdf", "image/png"], max_size=10MB, multiple=true, max_files=5))
```

---

## 5. Modifiers

Modifiers attach to elements (or containers) to control requirement, defaults, permissions, conditional behavior, layout hints, and UI metadata.

### 5.1 Inline Field Modifiers
- `*` (Required): placed directly before the label (`[*Name](...)`, `[ ] *Confirm (confirm)`).
- `=` (Default Value): placed inside `(...)` after `field_id[:type]` or inside `{...}` after `field_id[[]]`.

### 5.2 Trailing Element & Block Modifiers
Trailing modifiers appear after an element or on a `:::` block header in any order:

- **Permission Sigil**: `@(role_spec, ...)` — see Section 6.
- **Conditional Rule**: `when(expr)` — dynamically shows/enables the element or block when `expr` is truthy. Hidden elements are not validated or submitted unless explicitly persisted.
- **Read-Only / Disabled**: `readonly` or `disabled(expr)`.
- **Grid Span**: `span(int)` — number of columns spanned inside a `::: grid`.
- **Help / Placeholder**: `help("text")`, `placeholder("text")`.

**Example**:
```formspec
[*Termination Reason](termination_reason:textarea) when($status == "terminated") @(hr:write, *:none) help("Visible to HR only")
```

---

## 6. Permissions & Access Control

### 6.1 Permission Sigil Syntax

```
@( entry { , entry } )
entry = ( ident | "*" ) ":" ( "write" | "read" | "none" )
```

Example:
```formspec
@(hr:write, manager:read, *:none)
```

### 6.2 Permission Lattice

Permissions form a strict total order:

$$\text{none} \ (0) < \text{read} \ (1) < \text{write} \ (2)$$

- A role granted `write` automatically satisfies `read`.
- When a user holds **multiple roles**, the effective permission at any node is the **maximum** permission granted across all matching role entries (falling back to `*` only if no explicit role entry matches for that user).

### 6.3 Semantics by Element Kind

| Element Kind | `none` | `read` | `write` |
| :--- | :--- | :--- | :--- |
| **Container / Group** | Entire block and descendants hidden & excluded (unless a child explicitly overrides) | Block visible; descendants inherit `read` | Block visible; descendants inherit `write` |
| **Data Field** (input, choice, checkbox, file) | Hidden; excluded from client DOM, validation, and submission | Visible in read-only / non-editable mode; not user-modifiable | Visible and editable; validated & included in submission |
| **Button (`[[...]]`)** | Hidden and disabled; invocation rejected by server | **Visible and enabled** | **Visible and enabled** |

> **Important Rule for Submit & Action Buttons**:
> A submit button (or any `[[...]]` button) is **only visible and enabled to roles with `read` access** (including `write`, since `write >= read`). Roles resolving to `none` do not see the button and cannot invoke it.
>
> Consequently, if a form or section grants `manager:read` (so managers can view fields read-only) and managers should **not** be able to submit, explicitly override the button with `@(manager:none)` or `@(hr:write, *:none)`.

### 6.4 Block-Level Inheritance and Overrides

Permissions can be declared at the form level, on any container block (`::: section`, `::: group`, etc.), or on an individual element.

**Resolution Algorithm** for an element $E$ with ancestor chain $A_0 \to A_1 \to \dots \to E$:
1. Start with an empty role map (default fallback `*:write` if no root sigil is specified; or `*:none` in strict mode if configured by the runtime).
2. Walk from outermost ancestor $A_0$ down to $E$. At each node that declares `@(...)`:
   - If `*:perm` is declared at this node, reset `map` to `{"*": perm}` (re-baselining unmentioned inherited roles).
   - For each explicit `role:perm` in `@(...)`, set/overwrite `map[role] = perm`.
   - When `*` is **not** declared at this node, roles not mentioned retain their inherited value from the parent.
3. For a user with role set $R$:
   - Collect `{ map[r] | r in R and r in map }`.
   - If non-empty, effective permission is $\max$ of those values; otherwise `map["*"]` (or default).

**Example**:
```formspec
::: section "Compensation" @(hr:write, manager:read, *:none)
	[Base Salary](salary:currency)                          // hr:write, manager:read, *:none
	[*SSN / Tax ID](tax_id:text) @(manager:none)           // hr:write, manager:none, *:none
	[Manager Note](mgr_note:textarea) @(manager:write)     // hr:write, manager:write, *:none
	[[Approve]](approve) @(hr:write, manager:read, *:none) // visible & enabled to hr and manager
	[[Finalize Payroll]](finalize) @(hr:write, *:none)     // visible & enabled ONLY to hr
```

Notice that in `[Manager Note]`, overriding `manager:write` preserves `hr:write` and `*:none` from the enclosing `"Compensation"` block.

---

## 7. Grouping & Basic Layout

### 7.1 Multi-Line Grouping Blocks (`:::`) & Tab-Based Nesting

Multi-line grouping blocks begin with `:::` and group elements logically (permissions, `when(...)` visibility, repeatable arrays) and visually (sections, grids, rows, columns, tabs).

**Tab Nesting Rules**:
1. A `:::` header at tab depth `k` opens a multi-line block whose body consists of all immediately following lines at tab depth `>= k + 1`.
2. Direct child statements of that block MUST appear at tab depth `k + 1`.
3. A nested `:::` block inside it starts at tab depth `k + 1`, with its children at depth `k + 2`, and so on.
4. A non-blank, non-comment line at tab depth `<= k` (or end-of-file) implicitly closes the block—no closing `:::` marker is used.
5. Blank lines or lines inside balanced multi-line `(...)` / `{...}` expressions do not terminate the current block.

| Directive | Parameters | Purpose |
| :--- | :--- | :--- |
| `::: form <id> "Title"` | `(type: detail \| list)`, optional title | Root form container (`detail` by default) |
| `::: section "Title"` | Optional title, `collapsible` | Titled fieldset / card grouping |
| `::: group` | — | Headless logical group (permissions / `when`) |
| `::: row` | Optional `[r1:r2:...]`, `gap`, `align` | Multi-line horizontal row of elements or `::: col` blocks |
| `::: grid` | `cols`, `gap` | Multi-column grid (`span(N)` on children) |
| `::: col` | `width` | Explicit column inside a `row` |
| `::: tabs` / `::: tab "Title"` | — | Tabbed sub-sections |
| `::: repeat <field_id>` | `min`, `max`, `"Item Label"` | Repeatable array of sub-form items |
| `::: list <entity_query>` | Filter predicates, `.order(...)`, `.limit(...)` | Tabular record list for `list` forms |

**Multi-Line Grouping & Nested Tab Example**:
```formspec
::: section "Shipping Address"
	::: grid(cols: 2, gap: md)
		[*First Name](first_name:text)
		[*Last Name](last_name:text)
		[*Street Address](street:text) span(2)
		[*City](city:text)
		[*Postal Code](postal_code:text)
```

Multi-column vertical stacks can be expressed with `::: row` and nested `::: col` blocks:
```formspec
::: row [2:1]
	::: col
		[*Street](street:text)
		[*City](city:text) | [*Postal Code](postal_code:text) | [2:1]
	::: col
		[Delivery Instructions](notes:textarea(rows=4))
```

### 7.2 Pipe Layout Hints & Column Ratios (`|` and `| [ratios]`)

The pipe separator `|` arranges two or more elements horizontally on a single line as an implicit row. An optional trailing pipe may follow the last element, optionally followed by square brackets containing colon-separated width ratios:

```
element | element [| element ...] [| [[r1:r2[:...]]]]
```

- **Equal-width columns** (default when no ratio brackets are given):
  ```formspec
  [*First Name](first_name:text) | [*Middle](middle_name:text) | [*Last Name](last_name:text)
  ```
  (A trailing `|` without `[...]`—e.g. `[First](first) | [Last](last) |`—also defaults to equal ratios `1:1`.)
- **Proportional columns** via trailing `| [r1:r2:...]`:
  ```formspec
  [*Street Address](street:text) | [Apt / Suite](apt:text) | [3:1]
  [*City](city:text) | [*State]{state_id -> State} | [*ZIP](zip:text) | [2:1:1]
  ```
  - Each `r_i` is a positive integer or decimal weight (`int` or `number`).
  - Element $i$ receives fractional width $\frac{r_i}{\sum_j r_j}$ (e.g., `[3:1]` assigns `75%` and `25%`; `[2:1:1]` assigns `50%`, `25%`, `25%`).
  - The number of ratio segments inside `[...]` **must** equal the number of pipe-separated elements on the line (mismatch is a compile-time error).
  - Trailing `| [r1:r2:...]` is unambiguous from `[Label](id)` inputs because `[r1:r2:...]` appears after a trailing `|` and is not followed by `(...)` or `{...}`.

### 7.3 Repeatable Groups (`::: repeat`)

Repeatable blocks bind to an array field and allow adding/removing sub-records:

```formspec
::: repeat line_items(min: 1, max: 20) "Line Item"
	[*Item]{item_id -> CatalogItem(active == true)} | [*Qty](qty:int(min=1) = 1) | [Unit Price](unit_price:currency = $item_id.price) readonly | [3:1:1]
```

Inside a `::: repeat`, `$field_id` resolves first against the current repeat item scope, then falls back to enclosing form fields.

### 7.4 Detail Forms vs. List Forms (`type: detail | list` and `::: list`)

FormSpec distinguishes two complementary form types:
- **Detail Form (`type: detail`, default)**: Edits or displays a single record's fields. Detail forms carry a hidden primary key field (`[ID](id:hidden)`) and link their submit button to an upsert server action (`[[Submit]](submit:submit -> server_action:<entity>_upsert)`), which inserts when `id` is `null`/empty and updates when `id` is populated.
- **List Form (`type: list`)**: Displays an optionally filtered table of entity records via `::: list <entity_query>`, including hidden ID columns (`id`, `<fk>_id`) alongside resolved foreign-key display columns so parameters (`$row.<col>` or `$selected.<col>`) can be passed to linked forms or actions.

Inside a `::: list` block:
- Child field elements (`[Header](col[:type])`, `[Header]{fk_id -> target.key(id).display(name)}`) declare visible or `:hidden` table columns and inherit/override `@(...)` permissions just like detail fields (columns resolving to `none` are omitted for that role).
- Primary name/display columns can link directly to a detail form using `->` (e.g., `[Name](name:text -> user_form(id = $row.id))`).
- Selecting a row binds `$selected.<col>` so toolbar or action buttons (e.g., `[[Open Selected]](open -> user_form(id = $selected.id))`) can open forms or trigger `client_action`s using the selected record's values (including hidden IDs).
- Filter controls placed above `::: list` (e.g., `[Search](q:text)` or `[Department]{department_id -> ...}`) bind to `$field_id` predicates in the `::: list` query.

**Example List Form**:
```formspec
::: form user_list(type: list) "Users" @(admin:write, guest:read, *:none)
	[Search](q:text) placeholder("Search...") | [Department]{department_id -> department.key(id).display(name).order(name asc)} | [[Open Selected]](open -> user_form(id = $selected.id)) | [[+ New User]](create -> user_form(mode: "modal")) @(admin:write, *:none) | [2:2:1:1]

	::: list user(department_id == $department_id, username ~= $q).order(id asc)
		[ID](id:hidden)
		[Username](username:text -> user_form(id = $row.id))
		[Email](email:email)
		[Department ID](department_id:hidden)
		[Department]{department_id -> department.key(id).display(name)}
		[Active](active:boolean)
```

---

## 8. Entity Links & Scoped / Reactive Queries (`->`)

Choice fields (`{...}`)—and optionally derived default expressions—can bind dynamically to domain entities using `->`.

### 8.1 Basic Entity Link

```formspec
[Department]{dept_id -> Department}
```

- `dept_id` stores the entity's primary key (`Department.id` by default).
- The UI renders the entity's default display attribute (or explicit `.display(...)`).

### 8.2 Query Syntax

```ebnf
entity_query = query_root , { "." , query_stage } ;
query_root   = ident , [ "(" , predicate_list , ")" ]
             | field_ref , "." , ident , [ "(" , predicate_list , ")" ] ;
query_stage  = "order" "(" , order_spec , ")"
             | "limit" "(" , INT , ")"
             | "key" "(" , ident , ")"
             | "display" "(" , expr , ")"
             | "search" "(" , ident , { "," , ident } , ")"
             | ident , "(" , [ predicate_list ] , ")" ;
```

### 8.3 Scoped Entity Queries

Queries can be scoped by:
1. **Runtime context variables** (`$user`, `$org`, `$tenant`, `$today`, `$now`):
   ```formspec
   [Manager]{manager_id -> Employee(org_id == $user.org_id, role == "manager", active == true)}
   ```
2. **Named scopes / relationship traversal**:
   ```formspec
   [Project]{project_id -> $user.org.active_projects(archived == false)}
   ```
3. **Custom key/display projection & ordering**:
   ```formspec
   [Office]{office_code -> Office(active == true).key(code).display(city + " (" + country + ")").order(city asc)}
   ```
4. **Remote search & pagination** (for large datasets):
   ```formspec
   [Customer]{customer_id -> Customer(status == "active").search(name, email).limit(25)}
   ```

### 8.4 Queries Based on Other Form Elements (Reactive Dependencies)

Prefixing another form field's ID with `$` creates a reactive dependency:

**1. Predicate Filtering via Another Field**:
```formspec
[*Country]{country_id -> Country.order(name asc)}
[*State / Province]{state_id -> State(country_id == $country_id).order(name asc)}
[*City]{city_id -> City(state_id == $state_id).order(name asc)}
```

**2. Relationship Traversal from a Selected Entity Field**:
When `dept_id` is bound to `Department`, `$dept_id.<relation>` navigates the entity graph directly:
```formspec
[*Department]{dept_id -> Department(active == true)}
[*Team]{team_id -> $dept_id.teams(active == true).order(name asc)}
[Department Lead](lead_name:text = $dept_id.lead.full_name) readonly
```

**3. Multi-Field Conditions**:
```formspec
[Approver]{approver_id -> Employee(
    department_id == $dept_id,
    approval_limit >= $amount,
    id != $requester_id
).order(full_name asc)}
```

### 8.5 Reactive Dependency Semantics

1. **Dependency Graph**: The compiler builds a static DAG of `$field_id` references across entity queries, `= default` expressions, and `when(...)` rules. Circular dependencies are a compile-time error.
2. **Unset Upstream References**: If a query references `$x` and `$x` is `null` / unselected (unless `$x` is explicitly tested via `== null` or `!= null`), the dependent query evaluates to an empty option set `[]` and the dependent control enters a waiting/disabled state.
3. **Cascade Invalidations**: When `$country_id` changes, `state_id` re-evaluates its query. If the current value of `state_id` is not present in the new result set, `state_id` is reset to `null` (or its default if valid in the new set), which in turn cascades to `city_id`.
4. **Server Enforcement**: On submission, the server re-evaluates scoped queries and verifies that submitted entity IDs belong to the authorized, valid query result set for that user and form state.

---

## 9. Linking Forms, Parameters & Actions

FormSpec supports linking forms together for navigation, parameter passing into hidden fields, binding buttons to `server_action` and `client_action` handlers, inline entity creation, reusable sub-form embedding, and multi-step workflows.

### 9.1 Navigation, Parameter Passing & Hidden Field Injection

Buttons and linked list columns can open another form using `->` inside `(...)`:

```formspec
[[Label]](action_id[:link] -> target_form[(link_args)]) [modifiers...]
[Column Label](field_id[:type] -> target_form[(link_args)]) [modifiers...]
```

**Link Arguments & Parameter Injection**:
- **Passed Parameters**: Key-value pairs passing field values (`department_id = $department_id`), selected list row attributes (`id = $selected.id`), inline list row attributes (`id = $row.id`), or runtime context (`$user.id`).
- **Hidden Field Injection**: When a form is opened via a link with parameters (e.g., `user_form(id = $selected.id, department_id = $selected.department_id)`), each passed parameter is injected into the target form as a hidden field (`type: hidden`)—or populates an existing matching field such as `[ID](id:hidden)`—making it available to `$field_id` expressions and submitted payloads (`server_action`).
- **Declared Parameters**: A form header may optionally declare expected incoming parameters via `params: [id, department_id]` (e.g., `::: form user_form(type: detail, params: [id]) "User"`).
- `mode`: Presentation target — `"page"` (default navigation), `"modal"`, `"drawer"`, or `"tab"`.
- `returns`: Target field in the current form (e.g., `returns: $department_id`) to auto-populate with the linked form's submitted record `id` when a `"modal"` or `"drawer"` link completes, refreshing any dependent `->` entity queries.

### 9.2 Server Modules, Server Actions, Client Modules & Client Actions

Forms and entities separate reusable runtime logic from per-form and per-entity handlers:

- **Server Modules (`server_module`) & Server Actions (`server_action:<name>`)**:
  Shared server runtime helpers (RBAC resolution, filtered reads, FK lookup, transaction/upsert helpers) live in `server_module` (`formspec_server.mjs`). Creating a domain entity generates `<entity>_upsert` in `server_action`, which imports `./formspec_server.mjs`, inserts when `id` is `null`/empty and updates when `id` is populated, and enforces RBAC permissions server-side.
- **Client Modules (`client_module`) & Client Actions (`client_action:<name>`)**:
  Shared browser validation and dispatch helpers live in `client_module` (`formspec_client.mjs`). Creating a form generates a matching client action (`<form>_submit` for detail forms, `<form>_open` for list forms) importing `./formspec_client.mjs`. On detail forms, `[[Submit]](submit:submit -> client_action:<form>_submit)` invokes the client action to run client-side validation/checks before initiating `<entity>_upsert`:
  ```formspec
  [[Submit]](submit:submit -> client_action:user_form_submit) @(admin:write, *:none)
  ```
- Buttons can also link directly to a `server_action:<name>` or pass resolved parameters to a `client_action:<name>(...)`:
  ```formspec
  [[Open Selected]](open:link -> client_action:open_user_detail(id = $selected.id))
  ```

**Examples**:
```formspec
// Open a modal to create a new department, then auto-select it in department_id
[*Department]{department_id -> department.key(id).display(name).order(name asc)} | [[+ New]](new_dept -> department(mode: "modal", returns: $department_id)) | [4:1]

// Open an existing department record in a side drawer when one is selected
[[Edit Department]](edit_dept -> department(id = $department_id, mode: "drawer")) when($department_id != null)
```

### 9.3 Chained / Multi-Step Flows (Submit-and-Link)

When `:submit` is explicitly specified alongside `->`, the form first validates and submits its payload; on success, it navigates to the linked target form. Arguments may reference `$result.<field>` from the server action's response (such as the newly created record ID):

```formspec
[[Next: Payroll Setup]](next_payroll:submit -> payroll_setup(employee_id = $result.id)) when($emp_type == "full_time")
[[Next: Contractor Tax]](next_contractor:submit -> contractor_tax(employee_id = $result.id)) when($emp_type == "contractor")
```

- If validation or server submission fails, navigation **does not** occur and validation errors remain on the current form.

### 9.4 Embedded Sub-Forms (`::: embed`)

Reusable forms (such as shared address blocks or child line-item forms) can be embedded inline inside another form using `::: embed`:

```formspec
::: embed target_form[(embed_args)] [modifiers...]
```

**Embed Arguments**:
- `prefix`: Optional namespace prefix (e.g., `prefix: "billing"`) so the same form can be embedded multiple times without field ID collisions (`billing.street`, `shipping.street`).
- **Bound Parameters**: Field defaults or parent foreign keys passed into the embedded form (`order_id = $id`, `country_id = $country_id`).
- `independent`: Boolean (`false` by default). When `false`, the embedded form's fields validate and submit as a nested object (or array inside `::: repeat`) within the parent form; when `true`, the embedded form retains its own submit lifecycle.

**Examples**:
```formspec
::: section "Addresses"
	::: row [1:1]
		::: col
			### Billing
			::: embed address_form(prefix: "billing")
		::: col
			### Shipping
			::: embed address_form(prefix: "shipping") when(!$same_as_billing)

::: repeat order_items(min: 1) "Order Item"
	::: embed order_item_form(order_id = $id)
```

### 9.5 Permission, Validation & Cycle Rules for Linked Forms

1. **Button & Link Visibility**: Linked buttons (`[[...]]`) obey Section 6.3—visible and enabled only when the caller has `>= read` effective permission on the button. Runtimes should additionally disable or hide links whose target form denies the caller any `read`/`write` access.
2. **Embedded Permission Inheritance**: An `::: embed` block inherits the enclosing block's `@(...)` permissions unless overridden on the `::: embed` line or inside the embedded form (Section 6.4).
3. **Server Re-Verification**: Passed parameters (`id = $dept_id`, `employee_id = $result.id`) are client hints only; the target form's server action **must** enforce RBAC permissions and foreign-key validity independently on `load` and `submit`.
4. **Acyclic Embedding**: Circular `::: embed` graphs (e.g., `form_a` embeds `form_b` which embeds `form_a`) are a compile-time error.

---

## 10. Validation & Submission Lifecycle

1. **Initialization**: Evaluate default values (`=`, `[x]`), resolve user permissions, and run initial entity queries whose dependencies are satisfied.
2. **Interaction**: Update field state; re-evaluate `when(...)` conditions, dependent entity queries, and derived defaults.
3. **Client Submission** (`[[...]]` with `:submit`):
   - Verify caller has effective permission `>= read` on the clicked button.
   - Validate all visible, writable (`write`) fields for required (`*`) and type constraints (including non-independent `::: embed` sub-forms).
   - Exclude fields with `none` permission or hidden by `when(...)`.
4. **Server Verification**:
   - Recompute effective permissions for the authenticated user's roles.
   - Reject any attempt to invoke a button with `none` permission or mutate fields with `read` or `none` permission.
   - Re-validate constraints, upload limits/MIME types, and entity-query membership before committing.

---

## 11. Complete Example

```formspec
::: form employee_onboarding "Employee Onboarding" @(hr:write, manager:read, it:read, *:none)
	# New Hire Onboarding
	Complete the details below to provision payroll, department assignment, and IT access.

	::: section "Candidate Details"
		[*First Name](first_name:text(min=1, max=80)) | [*Last Name](last_name:text(min=1, max=80)) | [1:1]
		[*Personal Email](personal_email:email) | [*Start Date](start_date:date = $today) | [2:1]
		[*Resume](resume:file(accept=[".pdf", ".docx"], max_size=10MB))

	::: section "Organization & Role"
		[*Employment Type]{emp_type = full_time : full_time("Full-Time"), contractor("Contractor"), intern("Intern")}
		[*Department]{dept_id -> Department(org_id == $user.org_id, active == true).order(name asc)} | [[+ New Dept]](add_dept -> department(mode: "modal", returns: $dept_id)) @(hr:write, *:none) | [4:1]
		[*Team]{team_id -> $dept_id.teams(active == true).order(name asc)} | [*Manager]{manager_id -> Employee(department_id == $dept_id, is_manager == true, active == true).order(full_name asc)} | [1:1]
		[Compensation Band]{band_id -> CompBand(emp_type == $emp_type)} @(hr:write, manager:read, it:none)

	::: section "IT Provisioning" @(it:write, hr:write, manager:read, *:none)
		[Laptop Profile]{laptop = mac_pro : mac_pro("MacBook Pro"), linux_ws("Linux Workstation"), win_pro("Windows Laptop")}
		[Access Groups]{groups[] -> AccessGroup(department_id == $dept_id).order(name asc) | chips}
		[x] Provision corporate email on start date (provision_email)

	[ ] *I confirm all onboarding information is accurate (confirmed)

	[[Save Draft]](save_draft:action) @(hr:write, it:write, manager:none, *:none) | [[Submit & Provision]](submit:submit -> provisioning_summary(onboarding_id = $result.id)) @(hr:write, *:none) | [1:2]
```

---

## 12. Formal Grammar (EBNF Summary)

> `INDENT` and `DEDENT` tokens are emitted by the lexer from leading `\t` counts (increasing by 1 tab on `:::` block entry and decreasing on dedent; leading spaces are invalid).

```ebnf
document        = { statement } ;
statement       = comment
                | embed_stmt
                | block
                | divider
                | heading
                | row_line
                | element
                | prose_line ;

embed_stmt      = ":::" , "embed" , form_target , { modifier } , NEWLINE ;

block           = ":::" , ident , [ block_args ] , [ ratio_spec ] , { modifier } , NEWLINE ,
                  INDENT , { statement } , DEDENT ;
block_args      = [ ident ] , [ string ] , [ "(" , named_arg_list , ")" ] ;

row_line        = element , "|" , element , { "|" , element } , [ "|" , [ ratio_spec ] ] , NEWLINE ;
ratio_spec      = "[" , number , ":" , number , { ":" , number } , "]" ;

element         = ( input_elem
                  | choice_elem
                  | checkbox_elem
                  | button_elem ) , { modifier } ;

input_elem      = "[" , [ "*" ] , label_text , "]" ,
                  "(" , ident , [ ":" , type_spec ] , [ "=" , expr ] ,
                  [ "->" , link_target ] , ")" ;

choice_elem     = "[" , [ "*" ] , label_text , "]" ,
                  "{" , ident , [ "[]" ] , [ "=" , expr ] ,
                  ( ":" , option_list | "->" , entity_query ) ,
                  [ "|" , presentation_hint ] , "}" ;

checkbox_elem   = "[" , ( " " | "x" | "X" ) , "]" ,
                  [ "*" ] , label_text ,
                  "(" , ident , [ "=" , expr ] , ")" ;

button_elem     = "[[" , label_text , "]]" ,
                  "(" , ident , [ ":" , ( "submit" | "reset" | "action" | "link" ) ] ,
                  [ "->" , link_target ] , ")" ;

link_target     = action_target | form_target ;
action_target   = ( "server_action" | "client_action" ) , ":" , ident , [ "(" , [ named_arg_list ] , ")" ] ;
form_target     = ( ident | string ) , [ "(" , [ named_arg_list ] , ")" ] ;

modifier        = perm_sigil
                | "when" , "(" , expr , ")"
                | "span" , "(" , INT , ")"
                | "help" , "(" , string , ")"
                | "placeholder" , "(" , string , ")"
                | "readonly"
                | "disabled" , [ "(" , expr , ")" ] ;

perm_sigil      = "@" , "(" , perm_entry , { "," , perm_entry } , ")" ;
perm_entry      = ( ident | "*" ) , ":" , ( "none" | "read" | "write" ) ;

entity_query    = query_root , { "." , query_stage } ;
query_root      = ident , [ "(" , [ expr_list ] , ")" ]
                | field_ref , "." , ident , [ "(" , [ expr_list ] , ")" ] ;
query_stage     = ident , "(" , [ expr_list ] , ")" ;

field_ref       = "$" , ident , { "." , ident } ;
```
