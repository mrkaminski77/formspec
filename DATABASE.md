# FormSpec Supporting Database Requirements

This document defines the schema, naming, primary/foreign key, `entity` metadata, and RBAC conventions required by the FormSpec generator (`python3 -m formspec generate`) and its generated server-action scripts.

---

## 1. Naming & Key Conventions

### 1.1 Singular Entity Names
- Domain entity tables **must always be named in the singular** (e.g., `user`, `department`, `team`, `invoice_line`), never plural (`users`, `departments`).
- Use lowercase `snake_case` for both entity (table) names and attribute (column) names.

### 1.2 Primary Keys (`id`)
- Every entity table **must** use `id` as its primary key column:
  ```sql
  id INTEGER PRIMARY KEY AUTOINCREMENT
  ```
- By default, `id` is omitted from editable `.fspec` form inputs (pass `--include-pk` to include it) and used internally for record lookup (`record_id`) and `UPDATE` targeting.

### 1.3 Foreign Keys (`<entity>_id`)
- Foreign-key attributes referencing another entity **must** follow the `<entity>_id` naming pattern (e.g., `department_id` references `department.id`, `manager_id` references `manager.id` or a declared `FOREIGN KEY` target).
- When generating a form or serving `load` options for `<entity>_id`:
  - The target table is resolved from any declared SQL `FOREIGN KEY` constraint, or by stripping the `_id` suffix to obtain the singular `<entity>` table name.
  - The target table is queried for `(id, <display_attribute>)` ordered ascending by `<display_attribute>`.
  - By default, the referenced entity's `name` column is used for option labels (unless overridden by `display_attribute` in the `entity` metadata table).

---

## 2. The `entity` Metadata Table

Databases should include an `entity` table describing domain entities with labels and descriptions:

```sql
CREATE TABLE entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT
);
```

| Column | Role | Required | Notes |
| :--- | :--- | :--- | :--- |
| `id` | Primary key | Yes | Always `id`. |
| `name` | Relation / entity name | Yes | Exact table name of the singular entity (e.g., `'user'`, `'department'`). *(The SQLite adapter also recognizes `slug` if present.)* |
| `label` | Display name | Yes | Human-readable form title emitted in `.fspec` (`::: form <name> "<label>"`). *(Also recognizes `display_name` or `title` if present.)* |
| `description` | Entity description | Optional | Human-readable summary describing the entity's purpose in the application schema. |
| `display_attribute` | Option label column | Optional | Overrides the default `name` column when this entity is referenced by another table's `<entity>_id` foreign key. |

**Example rows**:
```sql
INSERT INTO entity (name, label, description) VALUES
    ('department', 'Department', 'Organizational departments and cost centers'),
    ('user', 'User Account', 'Application user profiles and department assignments');
```

---

## 3. RBAC Tables & Granular Permissions

### 3.1 Required RBAC Tables
The supporting database provides five RBAC tables:

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE
);

CREATE TABLE roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role_id INTEGER NOT NULL REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id),
    permission_id INTEGER NOT NULL REFERENCES permissions(id),
    PRIMARY KEY (role_id, permission_id)
);
```

> **Adapter flexibility**: `roles.name` and `permissions.name` are the standard columns (`slug` is also recognized if present). If a database uses singular RBAC table names (`user`, `role`, `permission`, `user_role`, `role_permission`), the SQLite adapter detects those automatically as a fallback, and `cli.py --all` excludes both plural and singular RBAC tables plus `entity` from domain form generation.

### 3.2 Permission Naming Format
Permissions follow one of two forms:

| Form | Pattern | Meaning |
| :--- | :--- | :--- |
| **Entity-level** | `<entity_name>:<access>` | Grants `<access>` (`read` or `write`) to **all** attributes of `<entity_name>`. |
| **Attribute-level** | `<entity_name>:<access>_<attribute>` | Grants granular `<access>` (`read` or `write`) to a single `<attribute>` on `<entity_name>`. |

**Examples**:
- `user:read` — grants `read` access to every attribute in the `user` table.
- `user:write` — grants `write` (and implicitly `read`) access to every attribute in the `user` table.
- `user:write_username` — grants `write` access specifically to the `username` attribute in `user`.
- `user:read_email` — grants `read` access specifically to the `email` attribute in `user`.

### 3.3 Permission Resolution Rules
- **Lattice**: `none < read < write` (`write` implies `read`).
- **Additive union**: For any user and attribute `attr`, the effective permission is the maximum access across all roles assigned to the user combining entity-level and attribute-level grants (`max(entity_perm, attr_perm)`).
- **Submit capability**: Only roles holding `write` on the entity or `write` on at least one attribute are granted access to `[[Submit]](submit)` in `.fspec` and permitted to invoke `submit` in the generated server action.
- **Server-side enforcement**:
  - `load`: Requires at least one readable attribute (`>= read`); fields with `none` are stripped from returned records and omitted from `_id` option queries.
  - `submit`: Rejects requests from roles without write access (`403`), rejects any payload attribute whose effective permission is not `write` (`403`), validates `NOT NULL` required fields and `<entity>_id` foreign-key existence (`422`), and executes parameterized `INSERT` / `UPDATE`.

---

## 4. SQL Type & Constraint Mapping

The SQLite adapter maps column definitions to FormSpec types and modifiers as follows:

| SQL Column Type / Name | FormSpec Control / Type | Notes |
| :--- | :--- | :--- |
| `<entity>_id` | `{col -> <entity>.key(id).display(<display_col>).order(<display_col> asc)}` | Choice populated from target `<entity>` table |
| `BOOLEAN` / `BOOL` | `[ ] Label (col)` / `[x] Label (col)` | Checkbox initialized from SQL default |
| `INT` / `INTEGER` / `BIGINT` / `SMALLINT` | `(col:int)` | Integer input |
| `REAL` / `FLOAT` / `DOUBLE` / `NUMERIC` / `DECIMAL` | `(col:decimal)` | Decimal input |
| `DATETIME` / `TIMESTAMP` | `(col:datetime)` | Timestamp input (`CURRENT_TIMESTAMP` -> `= $now`) |
| `DATE` | `(col:date)` | Date input (`CURRENT_DATE` -> `= $today`) |
| `TIME` | `(col:time)` | Time input |
| `BLOB` | `(col:file)` | File upload |
| `email` or `*_email` (`TEXT`/`VARCHAR`) | `(col:email)` | Email input |
| Other `TEXT` / `VARCHAR` | `(col:text)` | Text input |

- `NOT NULL` columns (except boolean checkboxes with a default) receive the `*` required prefix before the label.
- SQL column defaults are translated into `= <default>` expressions.

---

## 5. Complete Supporting Schema Example

```sql
CREATE TABLE entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT
);

CREATE TABLE department (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    department_id INTEGER NOT NULL REFERENCES department(id),
    active BOOLEAN NOT NULL DEFAULT 1
);

INSERT INTO entity (name, label, description) VALUES
    ('department', 'Department', 'Company departments'),
    ('user', 'User Account', 'System user accounts');

INSERT INTO roles (id, name) VALUES (1, 'admin'), (2, 'self_service'), (3, 'viewer');
INSERT INTO permissions (id, name) VALUES
    (1, 'user:write'),
    (2, 'user:read'),
    (3, 'user:write_username');

INSERT INTO role_permissions (role_id, permission_id) VALUES
    (1, 1),
    (2, 2),
    (2, 3),
    (3, 2);
```

Generating the `.fspec` form and server action(s) from this database:

```bash
python3 -m formspec generate --db sqlite:///app.db --entity user --target both --out-dir ./generated
```

---

## 6. IDE Application Tables (`forms`, `server_module`, `server_action`, `client_module`, `client_action`)

When initialized by the Prototype IDE (`python3 -m formspec ide`), the database also includes application-definition and shared module tables:

```sql
CREATE TABLE forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    entity_id INTEGER REFERENCES entity(id) ON DELETE SET NULL,
    form_type TEXT NOT NULL DEFAULT 'detail',
    fspec_source TEXT NOT NULL,
    formatter TEXT NOT NULL DEFAULT 'html5',
    theme TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE server_module (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    target_runtime TEXT NOT NULL DEFAULT 'node',
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE server_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    entity_id INTEGER REFERENCES entity(id) ON DELETE SET NULL,
    action_type TEXT NOT NULL DEFAULT 'upsert',
    target_runtime TEXT NOT NULL DEFAULT 'node',
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE client_module (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE client_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    target_form_id INTEGER REFERENCES forms(id) ON DELETE SET NULL,
    action_type TEXT NOT NULL DEFAULT 'link',
    params_spec TEXT,
    code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- **`forms`**: Stores `.fspec` form definitions (`form_type`: `'detail'` or `'list'`), optionally linked to an `entity(id)`.
- **`server_module`**: Stores reusable server runtime modules (`formspec_server` seeded on initialization) imported by generated server actions.
- **`server_action`**: Stores entity/custom permission-enforcing server scripts (`<entity>_upsert` auto-generated when an entity is created and synced as columns/FKs are added).
- **`client_module`**: Stores reusable client validation and dispatch helpers (`formspec_client` seeded on initialization) imported by client actions.
- **`client_action`**: Stores per-form client handlers (`<form>_submit` or `<form>_open` auto-generated when each form is created) that run client checks and initiate server actions or navigation.
