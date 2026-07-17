// Roslyn single-point mutation engine for C#/.NET repair-task generation.
//
// Each mutation is SYNTACTICALLY well-formed (parseable C#) but may be
// semantically wrong -> that is the point. The external verifier (dotnet
// build/test) decides whether a mutation is valid (compile error / test fail)
// and whether the gold reversal repairs it. We never guess outcomes here.
//
// CLI:
//   mutator enumerate <file.cs>                 -> JSON array of points
//   mutator apply <file.cs> <family> <index> <outfile>  -> JSON meta on stdout
//
// Families: compile, logic, async, linq, framework
// Logic points inside an async method are re-tagged as "async".
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

public static class Program
{
    static readonly Dictionary<string, SyntaxKind> OpKind = new()
    {
        {"==", SyntaxKind.EqualsEqualsToken}, {"!=", SyntaxKind.ExclamationEqualsToken},
        {"<",  SyntaxKind.LessThanToken},      {">",  SyntaxKind.GreaterThanToken},
        {"<=", SyntaxKind.LessThanEqualsToken},{">=", SyntaxKind.GreaterThanEqualsToken},
        {"+",  SyntaxKind.PlusToken},          {"-",  SyntaxKind.MinusToken},
        {"*",  SyntaxKind.AsteriskToken},      {"/",  SyntaxKind.SlashToken},
        {"&&", SyntaxKind.AmpersandAmpersandToken},{"||", SyntaxKind.BarBarToken},
    };

    // LINQ member renames that preserve compile types but flip behavior.
    static readonly Dictionary<string, string> LinqSwap = new()
    {
        {"Any","All"},{"All","Any"},
        {"First","FirstOrDefault"},{"FirstOrDefault","First"},
        {"Single","SingleOrDefault"},{"SingleOrDefault","Single"},
        {"OrderBy","OrderByDescending"},{"OrderByDescending","OrderBy"},
        {"Min","Max"},{"Max","Min"},
        {"Skip","Take"},{"Take","Skip"},
        {"Last","FirstOrDefault"},
        {"Contains","Any"},
    };
    static readonly HashSet<string> DiSet = new(){"AddSingleton","AddScoped","AddTransient"};
    static readonly Dictionary<string,string> DiSwap = new()
    {{"AddSingleton","AddScoped"},{"AddScoped","AddTransient"},{"AddTransient","AddSingleton"}};

    public static int Main(string[] args)
    {
        try
        {
            if (args.Length >= 2 && args[0] == "enumerate") return Enumerate(args[1]);
            if (args.Length >= 5 && args[0] == "apply")
                return Apply(args[1], args[2], int.Parse(args[3]), args[4]);
            Console.Error.WriteLine("usage: enumerate <file> | apply <file> <family> <index> <outfile>");
            return 2;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("MUTATOR-ERR: " + e);
            return 3;
        }
    }

    // ---------------- candidate ----------------
    sealed class Cand
    {
        public string family;
        public string desc;
        public int line;
        public Func<SyntaxNode, SyntaxNode> apply; // original root -> mutated root
    }

    static int LineOf(SyntaxTree tree, SyntaxNode n) =>
        tree.GetLineSpan(n.Span).StartLinePosition.Line + 1;
    static int LineOf(SyntaxTree tree, SyntaxToken t) =>
        tree.GetLineSpan(t.Span).StartLinePosition.Line + 1;

    static List<Cand> Collect(SyntaxTree tree)
    {
        var root = tree.GetRoot();
        var list = new List<Cand>();
        var w = new Walker(tree, list);
        w.Visit(root);
        return list;
    }

    sealed class Walker : CSharpSyntaxWalker
    {
        readonly SyntaxTree _tree;
        readonly List<Cand> _list;
        public Walker(SyntaxTree tree, List<Cand> l) : base(SyntaxWalkerDepth.Token) { _tree = tree; _list = l; }

        static bool InAsync(SyntaxNode n) =>
            n.FirstAncestorOrSelf<BaseMethodDeclarationSyntax>()?
              .Modifiers.Any(SyntaxKind.AsyncKeyword) == true;

        void Add(string fam, string desc, int line, Func<SyntaxNode, SyntaxNode> fn)
            => _list.Add(new Cand { family = fam, desc = desc, line = line, apply = fn });

        public override void VisitBinaryExpression(BinaryExpressionSyntax n)
        {
            var op = n.OperatorToken;
            var k = op.Kind();
            string to = k switch
            {
                SyntaxKind.EqualsEqualsToken      => "!=",
                SyntaxKind.ExclamationEqualsToken => "==",
                SyntaxKind.LessThanToken          => ">",
                SyntaxKind.GreaterThanToken       => "<",
                SyntaxKind.LessThanEqualsToken    => ">=",
                SyntaxKind.GreaterThanEqualsToken => "<=",
                SyntaxKind.PlusToken              => "-",
                SyntaxKind.MinusToken             => "+",
                SyntaxKind.AsteriskToken          => "/",
                SyntaxKind.SlashToken             => "*",
                SyntaxKind.AmpersandAmpersandToken=> "||",
                SyntaxKind.BarBarToken            => "&&",
                _ => null,
            };
            if (to != null && OpKind.ContainsKey(to))
            {
                string fam = InAsync(n) ? "async" : "logic";
                var k2 = OpKind[to];
                Add(fam, $"{k}->{to}", LineOf(_tree, op), r => r.ReplaceToken(op,
                    SyntaxFactory.Token(op.LeadingTrivia, k2, op.TrailingTrivia)));
            }
            base.VisitBinaryExpression(n);
        }

        public override void VisitLiteralExpression(LiteralExpressionSyntax n)
        {
            string fam = InAsync(n) ? "async" : "logic";
            if (n.IsKind(SyntaxKind.TrueLiteralExpression))
                Add(fam, "true->false", LineOf(_tree, n), r => r.ReplaceNode(n,
                    SyntaxFactory.LiteralExpression(SyntaxKind.FalseLiteralExpression)));
            else if (n.IsKind(SyntaxKind.FalseLiteralExpression))
                Add(fam, "false->true", LineOf(_tree, n), r => r.ReplaceNode(n,
                    SyntaxFactory.LiteralExpression(SyntaxKind.TrueLiteralExpression)));
            else if (n.IsKind(SyntaxKind.NumericLiteralExpression))
            {
                try
                {
                    long v = Convert.ToInt64(n.Token.Value, CultureInfo.InvariantCulture);
                    long nv = v + 1;
                    Add(fam, $"{v}->{nv}", LineOf(_tree, n), r => r.ReplaceNode(n,
                        SyntaxFactory.LiteralExpression(SyntaxKind.NumericLiteralExpression,
                            SyntaxFactory.Literal(nv))));
                }
                catch { /* non-integer literal: skip */ }
            }
            base.VisitLiteralExpression(n);
        }

        public override void VisitUsingDirective(UsingDirectiveSyntax n)
        {
            // removing a using directive -> CS0246/CS0103 when the namespace is referenced
            Add("compile", "remove using " + n.Name, LineOf(_tree, n),
                r => r.RemoveNode(n, SyntaxRemoveOptions.KeepNoTrivia) ?? r);
            base.VisitUsingDirective(n);
        }

        public override void VisitInvocationExpression(InvocationExpressionSyntax n)
        {
            if (n.Expression is MemberAccessExpressionSyntax ma && ma.Name is IdentifierNameSyntax idn)
            {
                var name = idn.Identifier.ValueText;
                var idTok = idn.Identifier;
                int line = LineOf(_tree, idn);

                if (LinqSwap.TryGetValue(name, out var lnew))
                {
                    Add("linq", $"{name}->{lnew}", line, RenameId(idTok, lnew));
                }
                else if (DiSwap.TryGetValue(name, out var dnew))
                {
                    Add("framework", $"{name}->{dnew}", line, RenameId(idTok, dnew));
                }
                else if (name.Length > 1 && !char.IsUpper(name[0]) == false && IsSimpleIdent(name))
                {
                    // member rename -> CS1061 (no such member) compile error
                    Add("compile", $"member {name}->{name}X", line, RenameId(idTok, name + "X"));
                }
            }
            base.VisitInvocationExpression(n);
        }

        public override void VisitAttribute(AttributeSyntax n)
        {
            if (n.Name is IdentifierNameSyntax an)
            {
                var aname = an.Identifier.ValueText;
                if (aname == "JsonPropertyName" || aname == "JsonProperty")
                {
                    if (n.ArgumentList?.Arguments.Count > 0)
                    {
                        var arg = n.ArgumentList.Arguments[0];
                        if (arg.Expression is LiteralExpressionSyntax lit &&
                            lit.IsKind(SyntaxKind.StringLiteralExpression))
                        {
                            var orig = lit.Token.ValueText;
                            var mut = orig + "_";
                            Add("framework", $"json '{orig}'->'{mut}'", LineOf(_tree, lit),
                                r => r.ReplaceNode(lit,
                                    SyntaxFactory.LiteralExpression(SyntaxKind.StringLiteralExpression,
                                        SyntaxFactory.Literal(mut))));
                        }
                    }
                }
            }
            base.VisitAttribute(n);
        }

        static bool IsSimpleIdent(string s) => s.Length > 1 && s.All(char.IsLetterOrDigit);

        static Func<SyntaxNode, SyntaxNode> RenameId(SyntaxToken tok, string to) => r =>
            r.ReplaceToken(tok, SyntaxFactory.Identifier(tok.LeadingTrivia, to, tok.TrailingTrivia));
    }

    // ---------------- commands ----------------
    static int Enumerate(string path)
    {
        var tree = Parse(path);
        var cands = Collect(tree);
        var out_list = cands.Select(c => new Dictionary<string, object>
        {
            ["family"] = c.family, ["desc"] = c.desc, ["line"] = c.line
        });
        Console.Write(JsonSerializer.Serialize(out_list));
        return 0;
    }

    static int Apply(string path, string family, int index, string outfile)
    {
        var tree = Parse(path);
        var cands = Collect(tree);
        var matches = cands.Where(c => c.family == family).ToList();
        var meta = new Dictionary<string, object>{ ["applied"] = false };
        if (index >= 0 && index < matches.Count)
        {
            var c = matches[index];
            var root = tree.GetRoot();
            var newRoot = c.apply(root);
            var oldText = root.ToFullString();
            var newText = newRoot.ToFullString();
            File.WriteAllText(outfile, newText);
            meta["applied"] = true;
            meta["family"] = c.family;
            meta["index"] = index;
            meta["line"] = c.line;
            meta["desc"] = c.desc;
            meta["old_text"] = oldText;
            meta["new_text"] = newText;
        }
        Console.Write(JsonSerializer.Serialize(meta));
        return 0;
    }

    static SyntaxTree Parse(string path)
    {
        var text = File.ReadAllText(path);
        var opts = new CSharpParseOptions(LanguageVersion.Preview);
        return CSharpSyntaxTree.ParseText(text, opts, path: path);
    }
}
