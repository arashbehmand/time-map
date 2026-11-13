# Convert TfL's Underground TransXChange files with UK2GTFS and record what happens.
# Args: <journey-planner-timetables.zip> <output folder>
args <- commandArgs(trailingOnly = TRUE)
zip_in <- args[1]
out <- args[2]
dir.create(out, recursive = TRUE, showWarnings = FALSE)
library(UK2GTFS)

tmp <- file.path(tempdir(), "tfl")
dir.create(tmp)
outer <- unzip(zip_in, exdir = tmp)
inner <- outer[grepl("LULDLRTRAMRIVERCABLE", basename(outer))]
xml <- unzip(inner, exdir = file.path(tmp, "metro"))
# Underground files are tfl_1-<line code>-...; the line codes of the 11 lines.
lines <- c("BAK", "CEN", "CIR", "DIS", "HAM", "JUB", "MET", "NTN", "PIC", "VIC", "WAC")
lu <- xml[grepl(paste0("tfl_1-(", paste(lines, collapse = "|"), ")-"), basename(xml))]
cat("Underground files:", length(lu), "\n")

started <- Sys.time()
warns <- character()
gtfs <- withCallingHandlers(
  transxchange2gtfs(path_in = lu, ncores = 4, try_mode = TRUE, silent = FALSE),
  warning = function(w) {
    warns <<- c(warns, conditionMessage(w))
    invokeRestart("muffleWarning")
  }
)
elapsed <- as.numeric(difftime(Sys.time(), started, units = "mins"))
if (is.list(gtfs) && !is.null(gtfs$trips)) {
  gtfs_write(gtfs, folder = out, name = "uk2gtfs_underground")
  status <- "converted"
} else {
  status <- "merge failed: returned a list of GTFS objects"
}
writeLines(c(paste("status:", status), paste("files:", length(lu)),
             paste("minutes:", round(elapsed, 1)), paste("warnings:", length(warns)), warns),
           file.path(out, "uk2gtfs_log.txt"))
cat("done:", status, "in", round(elapsed, 1), "min,", length(warns), "warnings\n")
