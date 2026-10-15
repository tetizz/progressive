(() => {
  "use strict";

  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const FILES = "abcdefgh";
  const PIECE_NAMES = Object.freeze({
    p: "pawn",
    n: "knight",
    b: "bishop",
    r: "rook",
    q: "queen",
    k: "king",
  });

  function parseFen(fen) {
    const text = String(fen || START_FEN).trim();
    const fields = text.split(/\s+/);
    const rows = (fields[0] || START_FEN.split(" ")[0]).split("/");
    const pieces = new Map();
    rows.slice(0, 8).forEach((row, rowIndex) => {
      let file = 0;
      for (const token of row) {
        if (/\d/.test(token)) {
          file += Number(token);
        } else if (file < 8 && PIECE_NAMES[token.toLowerCase()]) {
          const rank = 7 - rowIndex;
          pieces.set(`${FILES[file]}${rank + 1}`, {
            type: token.toLowerCase(),
            color: token === token.toUpperCase() ? "white" : "black",
          });
          file += 1;
        }
      }
    });
    return { pieces, turn: fields[1] === "b" ? "black" : "white" };
  }

  function pieceAsset(piece, base = "./pieces/cburnett") {
    const prefix = piece.color === "white" ? "w" : "b";
    return `${base}/${prefix}${piece.type.toUpperCase()}.svg`;
  }

  function squareName(file, rank) {
    return `${FILES[file]}${rank + 1}`;
  }

  function normalizedSquares(values) {
    return values instanceof Set ? values : new Set(values || []);
  }

  function model({
    fen = START_FEN,
    flipped = false,
    lastMove = null,
    selected = null,
    legalSources = [],
    legalDestinations = [],
    focusSquare = "e2",
    interactive = false,
  } = {}) {
    const { pieces } = parseFen(fen);
    const sources = normalizedSquares(legalSources);
    const destinations = normalizedSquares(legalDestinations);
    const rankOrder = flipped ? [0, 1, 2, 3, 4, 5, 6, 7] : [7, 6, 5, 4, 3, 2, 1, 0];
    const fileOrder = flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
    const lastFrom = lastMove?.slice(0, 2) || null;
    const lastTo = lastMove?.slice(2, 4) || null;
    const squares = [];

    rankOrder.forEach((rank, rowIndex) => {
      fileOrder.forEach((file, columnIndex) => {
        const name = squareName(file, rank);
        const piece = pieces.get(name) || null;
        const destination = destinations.has(name);
        const source = sources.has(name);
        const contents = piece ? `${piece.color} ${PIECE_NAMES[piece.type]}` : "empty square";
        const action = destination ? ", legal destination" : source ? ", movable" : "";
        squares.push({
          name,
          light: (file + rank) % 2 === 1,
          piece,
          source,
          selected: name === selected,
          last: name === lastFrom || name === lastTo,
          destination,
          capture: destination && piece !== null,
          fileLabel: rowIndex === 7 ? FILES[file] : null,
          rankLabel: columnIndex === 0 ? String(rank + 1) : null,
          tabIndex: interactive && name === focusSquare ? 0 : -1,
          ariaLabel: `${name}, ${contents}${action}`,
        });
      });
    });
    return squares;
  }

  function render(board, options = {}) {
    if (!board?.ownerDocument) throw new TypeError("board element is required");
    const document = board.ownerDocument;
    const fragment = document.createDocumentFragment();
    const squares = model(options);
    const pieceBase = options.pieceBase || "./pieces/cburnett";

    for (const square of squares) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `square ${square.light ? "light" : "dark"}`;
      button.dataset.square = square.name;
      button.tabIndex = square.tabIndex;
      button.setAttribute("aria-disabled", String(options.interactive !== true));
      if (square.piece) button.classList.add("has-piece");
      if (square.source) button.classList.add("is-legal-from");
      if (square.selected) button.classList.add("is-selected");
      if (square.last) button.classList.add("is-last");
      if (square.destination) {
        button.classList.add("is-legal");
        if (square.capture) button.classList.add("is-capture");
      }
      button.setAttribute("aria-label", square.ariaLabel);
      if (square.piece) {
        const image = document.createElement("img");
        image.className = `piece ${square.piece.color}`;
        image.src = pieceAsset(square.piece, pieceBase);
        image.alt = "";
        image.draggable = false;
        image.setAttribute("aria-hidden", "true");
        button.append(image);
      }
      if (square.fileLabel !== null) {
        const coordinate = document.createElement("span");
        coordinate.className = "coordinate file";
        coordinate.textContent = square.fileLabel;
        coordinate.setAttribute("aria-hidden", "true");
        button.append(coordinate);
      }
      if (square.rankLabel !== null) {
        const coordinate = document.createElement("span");
        coordinate.className = "coordinate rank";
        coordinate.textContent = square.rankLabel;
        coordinate.setAttribute("aria-hidden", "true");
        button.append(coordinate);
      }
      fragment.append(button);
    }
    board.replaceChildren(fragment);
    if (options.ariaLabel) board.setAttribute("aria-label", options.ariaLabel);
    return squares;
  }

  globalThis.ScottishProgressiveBoard = Object.freeze({
    FILES,
    PIECE_NAMES,
    model,
    parseFen,
    pieceAsset,
    render,
    squareName,
  });
})();
