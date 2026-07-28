# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-07-28

- bring in changes from upstream test suite (Jason Little)
- chore: use uv as the dependencies installer (FrenchGithubUser)
- feat: add endpoint to list the groups associated to a managed room (FrenchGithubUser)
- feat: add `operationId`s to the openAPI spec (FrenchGithubUser)
- feat: Allow deleting rooms from the Admin API to work even with managed rooms (Jason Little)
- feat: never partially create a managed room (FrenchGithubUser)
- feat: restrict who can call the famedly control API (FrenchGithubUser)
- fix: match openapi spec with code for `createRoom` request (FrenchGithubUser)
- test: add in test that makes sure assigning a group that does not exist(or other error received) doesn't actually modify the room (Jason Little)
- feat: migrate to service account to contact FC (FrenchGithubUser)

## [0.0.4] - 2026-05-26

- chore: update GitHub Action pins (Niklas Zender)
- feat: filtering and sorting for rooms (FrenchGithubUser)

## [0.0.3] - 2026-05-15

Updated the OpenAPI spec to reflect new API response shape. Added a retry queue for
joining/kicking users that have not been created yet. Bug fixes and performance improvements.

- chore: Adjust Trove classifiers to include Python 3.10 and 3.13 support (Jason Little)
- chore: correct comment and assertions (Jason Little)
- chore: Make the background sync loop more responsive (Jason Little)
- chore: refactor conditions and typing for values that can accept empty lists and appropriately shortcut out of work (Jason Little)
- chore: refactor get_users_room_membership() to retrieve a mapping of all of the interesting members instead of only one at a time (Jason Little)
- chore: Remove hatch scripts and replace with built-in hatch utilities (Jason Little)
- chore: Rename Membership to MembershipAction, to avoid confusion with the Synapse imported enum Membership (Jason Little)
- chore: short circuit out of adding and removing members if the list of members is empty (Jason Little)
- feat: Add a retry queue for errors encountered while changing membership in a room, when that user does not yet exist(and realign openapi-spec.yaml to reality) (Jason Little)
- feat: early return in sync loop if `sync_token` didn't change (FrenchGithubUser)
- fix: properly add auth header for famedly-control client requests (FrenchGithubUser)
- fix: Skip updating membership to a room if it is not a worthwhile update (Jason Little)
- fix: update sync behavior to handle user removal correctly (Soyoung Kim)
- refactor: Move business logic away from endpoint code and some misc fixes and improvements (Jason Little)
- test: verify that the retry queues retry attempt count works in testing (Jason Little)
- tests: add regression test for improper authorization header (FrenchGithubUser)
- tests: Adjust assertions to reflect that the error should be an empty dictionary instead of a general Falsey value (Jason Little)

## [0.0.2] - 2026-03-17

- chore: Stricter control on power levels during room creation and after, and more! [#30](https://github.com/famedly/famedly-control-synapse/pull/30)
- docs: document the module [#28](https://github.com/famedly/famedly-control-synapse/pull/28)
- fix: prohibit overriding specific power levels during room creation [#29](https://github.com/famedly/famedly-control-synapse/pull/29)
- tests: Remove patching for batched external user id calls [#24](https://github.com/famedly/famedly-control-synapse/pull/24)
- Add some niche power level tests and simplify defaults logic a tiny bit [#15](https://github.com/famedly/famedly-control-synapse/pull/15)
- chore: return proper error code for client response [#27](https://github.com/famedly/famedly-control-synapse/pull/27)
- fix: extra slash in constructed client urls [#25](https://github.com/famedly/famedly-control-synapse/pull/25)
- fix: Don't clobber other users in `power_level_content_override` [#26](https://github.com/famedly/famedly-control-synapse/pull/26)

## [0.0.1] - 2026-03-10

- Initial release
