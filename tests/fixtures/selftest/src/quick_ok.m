% quick_ok.m - 自检用短任务：写一个小 CSV 后正常退出
outdir = fullfile(pwd, 'results');
if ~exist(outdir, 'dir'), mkdir(outdir); end
T = table((1:3)', (1:3)'.^2, 'VariableNames', {'x', 'y'});
writetable(T, fullfile(outdir, 'quick.csv'));
fprintf('QUICK_OK\n');