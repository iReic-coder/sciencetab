% bad_validation.m - 自检：写出 Pass=false 的验证记录（应判 validation_failed）
outdir = fullfile(pwd, 'results');
if ~exist(outdir, 'dir'), mkdir(outdir); end
Check = ["only_check"]; Value = 0; Threshold = 1; Pass = false;
writetable(table(Check, Value, Threshold, Pass), fullfile(outdir, 'validation.csv'));
fprintf('BAD_VALIDATION_WRITTEN\n');