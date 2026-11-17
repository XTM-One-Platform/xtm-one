# SPDX-License-Identifier: Apache-2.0

"""What a library page lists: the one filter rule of the paginated libraries.

The presentations and the Video Studio's videos are read a page at a time,
filtered and searched in SQL, so every row stays reachable whatever the
library's size. Both apply the same rule, defined here:

* the rows the caller can open, three-tier WITHOUT the admin bypass: a library
  list never takes it, its pills could not account for a stranger's
  private row;
* narrowed by the visibility filter: ``mine`` (the caller's own, not published
  to the company), ``shared`` (visible, neither the caller's nor the
  company's: someone shared it, an account since deleted included, whose
  ``user_id`` is NULL) or ``company`` (published to the company);
* searched word by word: every word must be found, any case, in the title, the
  owner's name or the name of the template the row wears, as its card shows
  them. A template is matched by name only when the caller may see it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sqlalchemy import or_, select

from app.models.template_container import TemplateContainer
from app.models.user import User

if TYPE_CHECKING:
    from app.models.presentation import Presentation
    from app.models.video_studio import VideoProject

LibraryVisibility = Literal["all", "mine", "shared", "company"]
#: The words of a search that are matched; the rest is ignored.
MAX_SEARCH_WORDS = 8


def search_words(search: str | None) -> list[str]:
    """The words a search matches, every one required."""
    return (search or "").split()[:MAX_SEARCH_WORDS]


def library_filters(
    model: type[Presentation] | type[VideoProject],
    user: User,
    groups: list,
    *,
    search: str | None = None,
    visibility: LibraryVisibility = "all",
) -> list:
    """The SQL predicates of the rows of *model* that *user* can open and that match *search* and *visibility*."""
    from app.api.v1.helpers import user_visible_filter

    where = [user_visible_filter(model, user.id, groups, is_admin=False)]
    if visibility == "mine":
        where += [model.user_id == user.id, model.is_company_managed.is_not(True)]
    elif visibility == "shared":
        where += [
            or_(model.user_id.is_(None), model.user_id != user.id),
            model.is_company_managed.is_not(True),
        ]
    elif visibility == "company":
        where.append(model.is_company_managed.is_(True))
    for word in search_words(search):
        owners = select(User.id).where(User.full_name.icontains(word, autoescape=True))
        worn = select(TemplateContainer.id).where(
            TemplateContainer.name.icontains(word, autoescape=True),
            user_visible_filter(TemplateContainer, user.id, groups, is_admin=user.role == "admin"),
        )
        where.append(
            or_(
                model.title.icontains(word, autoescape=True),
                model.user_id.in_(owners),
                model.template_container_id.in_(worn),
            )
        )
    return where
