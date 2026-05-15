// /events/[id] is the user-facing Event URL per the 2026-05-14
// "Make The Record Actually Readable" sprint. The data layer still
// calls these episodes for stability; this route is just an alias.
// New links should point at /events/[id]; /episode/[id] keeps
// working for back-compat (existing bookmarks, audit links).
export { default } from "../../episode/[id]/page";
